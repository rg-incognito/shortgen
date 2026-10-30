"""
Speaker-aware 9:16 reframing.

Pipeline:
  1. pyannote.audio  — who speaks when (optional, needs HF_TOKEN)
  2. MediaPipe FaceLandmarker — face boxes + lip openness per sampled frame,
     plus a cheap frame-difference camera-cut detector on every frame
  3. Per-shot layout — face-tracked full-bleed crop; for wide multi-person
     shots (wide_fallback profiles) a stacked two-face split, or a blurred-
     background fill when no face can be found. Never black bars.
  4. Crop path — holds on detection misses, keeps a speaker for >= 0.8 s
     before switching, snaps (no pan) on camera cuts and speaker switches
  5. ffmpeg pipe — renders video-only 1080x1920 frames; the area under the
     hook banner (top_reserved px) is left black for the banner to cover

Templates (layout=, from the profile's render config; 'legacy' = the above):
  A1  sitcom / reality: a 1080x1350 (4:5) window at y 300 over a blurred,
      darkened copy of the same frame; two-shots widen the window (1:1, then
      the full source width) so both people stay in frame
  A2 / B1  full-bleed 1080x1920; the face is zoomed to ~1/4 of the canvas
      height (capped 1.35x) with the eye line near y 540
  B1H  B1 with the edit layer's hook headline: a 1080x1620 window at y 300 under
      a blurred band (the A1 look, so the headline sits in a top bar over no face)
Frames are never letterboxed with black bars.

Edit layer (clipper's pace plan): zoom_events carry tight reframe cuts (1.3-1.4x of
the same take, face-centred); zoom_max keeps wide x close under the source's
resolution limit by easing the wide framing first. flash: a light wash per frame.

Letterboxed sources: black bars baked into the source (a 2.39:1 film inside
16:9, a 4:3 pillarbox, an episode that letterboxes some segments) are found
per shot from the frames the detection pass already reads (content_box) and
cropped off before any layout, so face placement, zoom and the blurred fill
all work on the real picture. A source without bars gets the full frame.
"""
import os
import subprocess
import tempfile
import urllib.request
import numpy as np
import cv2
import mediapipe as mp
from mediapipe.tasks import python as _mp_python
from mediapipe.tasks.python import vision as _mp_vision

_MODEL_URL  = ('https://storage.googleapis.com/mediapipe-models/'
               'face_landmarker/face_landmarker/float16/1/face_landmarker.task')
_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '..', 'face_landmarker.task')


def _ensure_landmarker_model():
    if not os.path.exists(_MODEL_PATH):
        print('  Downloading face_landmarker.task (~6 MB)...')
        urllib.request.urlretrieve(_MODEL_URL, _MODEL_PATH)
        print('  Done.')


def _create_face_landmarker():
    _ensure_landmarker_model()
    options = _mp_vision.FaceLandmarkerOptions(
        base_options=_mp_python.BaseOptions(
            model_asset_path=os.path.abspath(_MODEL_PATH)),
        running_mode=_mp_vision.RunningMode.IMAGE,
        num_faces=6,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    return _mp_vision.FaceLandmarker.create_from_options(options)

_OUT_W, _OUT_H = 1080, 1920
HOOK_BAR_HEIGHT = 320  # keep in sync with captioner._HOOK_BAR_HEIGHT
_SAMPLE_FPS   = 4      # face-detection samples per second of source video
_SMOOTH_ALPHA = 0.18   # per-frame EMA inside a shot (~0.2 s time constant)
_DEAD_ZONE    = 0.012  # fraction of source width: ignore smaller target moves
_SWITCH_HOLD_S = 0.8   # another face must win this long before the crop moves to it
_SAME_FACE_DX = 0.10   # normalised cx distance treated as "the same face"
_PUSH_IN      = 0.04   # slow push-in amount over a long part (4%)

# Camera-cut detector: mean abs difference of 64x36 grey thumbnails (0-255).
_CUT_MAD      = 30.0
# Cuts this close to either end of a part are treated as flash frames.
_FLASH_FRAMES = 8

# Letterbox detection, per sampled frame on the grey picture. A bar row (or
# column) has a near-black median and almost no bright pixels (burned-in
# subtitles inside a bar stay under _BAR_SPARSE). Per shot, a row is a bar when
# _BAR_AGREE of the shot's lit samples agree AND its median luma is steady over
# time (moving dark content is not); only runs touching the frame edge count.
_BAR_LUMA = 24
_BAR_BRIGHT = 40
_BAR_SPARSE = 0.20             # burned subtitles / logos in a bar cover ~10-15% of their rows
_BAR_FLAT = 2.5                # max std of a bar line's dark pixels (bars measure 0-1.3)
_BAR_GAP = 4                   # non-bar lines bridged inside a bar (a logo's soft edge)
_BAR_AGREE = 0.75
_BAR_AGREE_LOOSE = 0.5         # layout fallback: crop anything that is black half the time
_BAR_STEADY = 3.0              # max temporal std of a bar row's median luma
_BAR_MIN = 4                   # px; thinner edges are left alone
_BAR_MAX = 0.30                # per side, share of the dimension (more = dark content)
_BAR_MARGIN = 2                # px also trimmed past the bar (soft / dim edge rows)

# Stacked split (wide_fallback profiles): two faces each at least this large
# relative to the bigger one, too far apart to share one crop.
_SPLIT_MIN_REL = 0.55
_SPLIT_MIN_CROP = 0.35   # split panel crop height, min fraction of the frame

# Templates. A1: 4:5 window over a blurred cover of the frame.
A1_WINDOW = (300, 1350)        # y, height of the default 4:5 window
A1_SQUARE = (420, 1080)        # two-shot window (1:1)
A1_BAND_MID = 975              # centre of the window band (300..1650)
B1H_WINDOW = (300, 1620)       # B1 + headline: the picture under the 300 px headline band
_WINDOWED = ('A1', 'B1H')      # templates that draw a window over a blurred cover
FLASH_RGB = (255, 243, 214)    # warm cream wash (#FFF3D6), EDIT_SPEC R6
_A1_ZOOM_MAX = 1.15            # single-face zoom cap inside the A1 window
_A1_FACE_TARGET = 0.20         # face height / canvas the A1 zoom aims at
_A1_EYE = 0.30                 # eye line at this share of the window height
_BG_DIM = 0.60                 # A1 background brightness
# Full-bleed (A2 / B1): face height target as a share of the 1920 canvas.
_FB_FACE_TARGET = 0.25
_FB_ZOOM_MAX = 1.35
_FB_EYE = 0.28                 # eye line at ~y 540
_HEAD_TOP_MIN = 140            # px: forehead stays below the Shorts icon row
_EYE_L, _EYE_R = 159, 386      # upper-eyelid landmarks

# MediaPipe Face Mesh landmark indices
_NOSE_TIP  = 1
_LIP_TOP   = 13
_LIP_BOT   = 14
_FACE_TOP  = 10
_FACE_BOT  = 152


def load_diarization_pipeline(hf_token: str):
    from pyannote.audio import Pipeline
    print('  Loading pyannote diarization model...')
    pipe = Pipeline.from_pretrained('pyannote/speaker-diarization-3.1')
    return pipe


def probe_fps(video_path):
    """(exact rate string for ffmpeg e.g. '24000/1001', float fps)."""
    p = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=r_frame_rate',
                        '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
                       capture_output=True, text=True)
    rate = p.stdout.strip().splitlines()[0] if p.stdout.strip() else ''
    try:
        num, den = (rate.split('/') + ['1'])[:2]
        fps = float(num) / float(den)
        if fps > 0:
            return rate, fps
    except (ValueError, ZeroDivisionError):
        pass
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    cap.release()
    return str(fps), fps


def _extract_wav(video_path, start_sec, end_sec, wav_path):
    subprocess.run([
        'ffmpeg', '-y',
        '-ss', str(start_sec), '-to', str(end_sec),
        '-i', video_path,
        '-vn', '-ar', '16000', '-ac', '1', wav_path,
    ], check=True, capture_output=True)


def _speech_frames(pipeline, wav_path, src_fps) -> set:
    """Frames (0-indexed within clip) that contain active speech."""
    result = pipeline(wav_path)
    frames = set()
    for seg, _, _ in result.itertracks(yield_label=True):
        for fi in range(int(seg.start * src_fps), int(seg.end * src_fps) + 1):
            frames.add(fi)
    return frames


def _dark_lines(px, axis):
    """(medians, bar-like flags) of the lines of px (axis=1: rows, axis=0: columns)."""
    med = np.median(px, axis=axis)
    d = px.astype(np.float32)
    m = d <= _BAR_LUMA
    n = np.maximum(m.sum(axis=axis), 1)
    mean = (d * m).sum(axis=axis) / n
    std = np.sqrt(np.maximum((d * d * m).sum(axis=axis) / n - mean * mean, 0))
    # a bar is flat black (its dark pixels barely vary); dark clothes / shadows have texture
    flags = (med <= _BAR_LUMA) & (std <= _BAR_FLAT) & ((px > _BAR_BRIGHT).mean(axis=axis) < _BAR_SPARSE)
    return med.astype(np.uint8), flags


def _bar_profile(gray):
    """(row medians, col medians, dark rows, dark cols, lit) of one grey frame."""
    rmed, rows = _dark_lines(gray[:, ::4], 1)
    # columns are judged inside the row bars only (a letterbox would make every
    # column through a dark suit look like a pillarbox bar)
    top, bot = _edge_run(rows), _edge_run(rows[::-1])
    body = gray[top:len(rows) - bot:4] if top + bot < len(rows) * 0.8 else gray[::4]
    cmed, cols = _dark_lines(body, 0)
    # a fade to black / a near-black frame says nothing about bars
    lit = (~rows).mean() > 0.4 and (~cols).mean() > 0.4
    return rmed, cmed, rows, cols, bool(lit)


def _edge_run(flags, gap=_BAR_GAP):
    """Length of the run of True from the start of flags, bridging up to `gap` False
    lines (the anti-aliased edge of a logo or subtitle drawn inside a bar)."""
    n = miss = 0
    if not len(flags) or not flags[0]:
        return 0
    for i, f in enumerate(flags):
        if f:
            n, miss = i + 1, 0
        else:
            miss += 1
            if miss > gap:
                break
    return n


def content_box(profiles, src_w, src_h, agree=_BAR_AGREE):
    """
    (x0, y0, x1, y1) of the real picture in source px, from _bar_profile()s of
    one shot: letterbox / pillarbox bars that are consistent across the shot
    are cut off. No bars (or nothing lit to judge by) -> the full frame.
    """
    lit = [p for p in profiles if p[4]]
    if not lit:
        return (0, 0, src_w, src_h)
    sides = []
    for med_i, dark_i, dim in ((0, 2, src_h), (1, 3, src_w)):
        dark = np.mean([p[dark_i] for p in lit], axis=0) >= agree
        if len(lit) > 1:
            # a bar never changes; dark moving content does (the loose fallback skips this)
            steady = np.std([p[med_i].astype(np.float32) for p in lit], axis=0) <= _BAR_STEADY
            if agree >= _BAR_AGREE:
                dark &= steady
        b = _bar_pair(_edge_run(dark), _edge_run(dark[::-1]), dim)
        sides.append(b + _BAR_MARGIN if b else 0)
    top, left = sides
    return (left, top, src_w - left, src_h - top)


def _bar_pair(lead, trail, dim):
    """
    The bar size to crop from BOTH ends of one axis, or 0. Letterbox / pillarbox
    bars come in pairs of about the same size; a dark band on one side only is
    crushed-black picture (a shadow, a dark suit, a night sky), not a bar. The
    larger side wins (a subtitle or logo inside a bar can shorten the other).
    """
    ok = lambda n: _BAR_MIN <= n <= dim * _BAR_MAX
    if not (ok(lead) and ok(trail)) or abs(lead - trail) > max(_BAR_MIN * 2, 0.35 * max(lead, trail)):
        return 0
    return max(lead, trail)


def _edges(p, src_w, src_h):
    """One sample's own bar sizes (rows, cols) to crop from each end, 0 = none."""
    return (_bar_pair(_edge_run(p[2]), _edge_run(p[2][::-1]), src_h),
            _bar_pair(_edge_run(p[3]), _edge_run(p[3][::-1]), src_w))


def _cover(b1, b2):
    """The tighter of two boxes (crops whatever either one crops)."""
    return (max(b1[0], b2[0]), max(b1[1], b2[1]), min(b1[2], b2[2]), min(b1[3], b2[3]))


def shot_boxes(an, agree=_BAR_AGREE):
    """
    [(a, b, box)] covering an analyse_clip() result's frames: one entry per camera
    shot (box: content_box of its samples), split further where the bars change
    inside a shot (a source that animates its letterbox open or shut). A sample
    in the middle of such a change takes the tighter neighbouring box.
    """
    src_w, src_h = an['src_w'], an['src_h']
    full = (0, 0, src_w, src_h)
    bars = an.get('bars') or {}
    whole = content_box(list(bars.values()), src_w, src_h, agree)
    out = []
    for a, b in _shots(an['cuts'], an['total_frames']):
        lit = [f for f in sorted(bars) if a <= f < b and bars[f][4]]
        if len(lit) < 2:
            out.append((a, b, whole))          # too short to judge: the part's verdict
            continue
        runs = []                              # consecutive samples with the same bars
        for f in lit:
            e = _edges(bars[f], src_w, src_h)
            if runs and max(abs(x - y) for x, y in zip(e, runs[-1][0])) <= _BAR_MIN:
                runs[-1][1].append(f)
            else:
                runs.append((e, [f]))
        if len(runs) == 1:
            out.append((a, b, content_box([bars[f] for f in lit], src_w, src_h, agree)))
            continue
        boxes = [content_box([bars[f] for f in fs], src_w, src_h, agree) if len(fs) >= 2 else None
                 for _, fs in runs]
        if not any(boxes):                     # bars never settle: judge the shot as a whole
            out.append((a, b, content_box([bars[f] for f in lit], src_w, src_h, agree)))
            continue
        for k, bx in enumerate(boxes):
            if bx is None:                     # a transition sample: crop like the tighter neighbour
                near = [boxes[j] for j in (k - 1, k + 1) if 0 <= j < len(boxes) and boxes[j]]
                boxes[k] = near[0] if len(near) == 1 else _cover(*near) if near else full
        seg = []
        for k, ((_, fs), bx) in enumerate(zip(runs, boxes)):
            start = a if k == 0 else fs[0]
            if seg and seg[-1][2] == bx:
                continue
            if seg:
                seg[-1][1] = start
            seg.append([start, b, bx])
        out += [tuple(s) for s in seg]
    return out


_FACE_X = ('cx', 'x0', 'x1', 'bx')
_FACE_Y = ('cy', 'top', 'bot', 'ey')


def _local_faces(faces, box, src_w, src_h):
    """Face dicts re-normalised to the content box (positions and sizes; lip etc. kept)."""
    x0, y0, x1, y1 = box
    if (x0, y0, x1, y1) == (0, 0, src_w, src_h):
        return faces
    bw, bh = (x1 - x0) / src_w, (y1 - y0) / src_h
    ox, oy = x0 / src_w, y0 / src_h
    out = []
    for f in faces:
        g = dict(f)
        for k in _FACE_X:
            if k in g:
                g[k] = (g[k] - ox) / bw
        for k in _FACE_Y:
            if k in g:
                g[k] = (g[k] - oy) / bh
        g['w'] = f['w'] / bw
        g['h'] = f['h'] / bh
        out.append(g)
    return out


def _sample_faces(video_path, start_f, total_frames, src_fps, tiled=False, detect=True):
    """
    Reads every frame of the clip once.

    Returns (faces, cuts, bars):
      faces: {clip_frame_idx: [face, ...]} sampled at _SAMPLE_FPS plus the first
             frame after every cut; face = dict(cx, cy, h, w, lip) normalised 0-1
             to the full source frame ({} when detect=False)
      cuts:  sorted clip frame indices where a new camera shot starts
      bars:  {clip_frame_idx: _bar_profile} on the same samples (letterbox check)
    """
    step = max(1, round(src_fps / _SAMPLE_FPS))

    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)

    landmarker = _create_face_landmarker() if detect else None
    data, cuts, bars = {}, [], {}
    prev_thumb = None
    last_sample = -step
    try:
        for fi in range(total_frames):
            ret, frame = cap.read()
            if not ret:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            thumb = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA).astype(np.int16)
            is_cut = (prev_thumb is not None
                      and float(np.mean(np.abs(thumb - prev_thumb))) > _CUT_MAD)
            prev_thumb = thumb
            if is_cut:
                cuts.append(fi)
            if not (is_cut or fi - last_sample >= step):
                continue
            last_sample = fi
            bars[fi] = _bar_profile(gray)
            if landmarker is None:
                continue
            rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            faces = _detect(landmarker, rgb)
            if not faces and tiled:
                faces = _detect_tiled(landmarker, rgb)
            data[fi] = faces
    finally:
        cap.release()
        if landmarker is not None:
            landmarker.close()
    return data, cuts, bars


def _detect(landmarker, rgb, box=None):
    """Faces in rgb (or in the sub-rect box=(x, y, w, h)), normalised to the full frame."""
    H, W = rgb.shape[:2]
    x0, y0, bw, bh = box or (0, 0, W, H)
    sub = np.ascontiguousarray(rgb[y0:y0 + bh, x0:x0 + bw])
    result = landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=sub))
    faces = []
    for face_lms in result.face_landmarks:
        xs = [(x0 + lm.x * bw) / W for lm in face_lms]
        ys = [(y0 + lm.y * bh) / H for lm in face_lms]
        lip_open = abs(face_lms[_LIP_BOT].y - face_lms[_LIP_TOP].y)
        face_h   = abs(face_lms[_FACE_BOT].y - face_lms[_FACE_TOP].y) or 0.1
        faces.append({'cx': (x0 + face_lms[_NOSE_TIP].x * bw) / W,
                      'cy': (min(ys) + max(ys)) / 2,
                      'h': max(ys) - min(ys), 'w': max(xs) - min(xs),
                      'top': min(ys), 'bot': max(ys), 'x0': min(xs), 'x1': max(xs),
                      'bx': (min(xs) + max(xs)) / 2,
                      'ey': (y0 + (face_lms[_EYE_L].y + face_lms[_EYE_R].y) / 2 * bh) / H,
                      'lip': lip_open / face_h})
    return faces


def _detect_tiled(landmarker, rgb):
    """
    Second pass on a detection miss: the landmarker's short-range detector
    loses faces smaller than ~1/6 of the frame height (wide podcast tables,
    two-shots). Re-run it on overlapping square tiles of 0.6 x frame height so
    those faces are proportionally bigger, then drop duplicates.
    """
    H, W = rgb.shape[:2]
    t = int(H * 0.6)
    xs = list(range(0, max(1, W - t), t // 2)) + [W - t]
    ys = list(range(0, max(1, H - t), t // 2)) + [H - t]
    found = []
    for y in sorted(set(ys)):
        for x in sorted(set(xs)):
            for f in _detect(landmarker, rgb, (x, y, t, t)):
                if all(abs(f['cx'] - g['cx']) > g['w'] * 0.5 or abs(f['cy'] - g['cy']) > g['h'] * 0.5
                       for g in found):
                    found.append(f)
    return found


def _shots(cuts, total_frames):
    bounds = [0] + [c for c in cuts if 0 < c < total_frames] + [total_frames]
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]


def _split_pair(faces, crop_w_norm):
    """Two biggest faces (left, right) if they form a wide two-person shot."""
    if len(faces) < 2:
        return None
    a, b = sorted(faces, key=lambda f: f['h'], reverse=True)[:2]
    if b['h'] < a['h'] * _SPLIT_MIN_REL or abs(a['cx'] - b['cx']) < crop_w_norm * 0.7:
        return None
    return (a, b) if a['cx'] < b['cx'] else (b, a)


def _shot_layout(samples, wide_fallback, crop_w_norm):
    """'crop' | 'split' | 'wide' for one shot, by majority over its samples."""
    if not wide_fallback or not samples:
        return 'crop'
    n = len(samples)
    if sum(1 for faces in samples if _split_pair(faces, crop_w_norm)) * 2 >= n:
        return 'split'
    if sum(1 for faces in samples if faces) * 3 < n:
        return 'wide'
    return 'crop'


def _track_shot(sample_items, speech_frames, fps, key='cy', tracked=None, xkey='cx'):
    """
    Choose who the crop follows at each sample of one shot.

    Returns [(frame, cx, y, switched)] with cx / y (face[key]) normalised;
    misses hold the last position and samples before the first detection take
    the first face. tracked: optional list that receives the followed faces.
    """
    hold = max(1, int(round(_SWITCH_HOLD_S * fps)))
    prev_faces = []
    cur = None
    pending_since = None
    out = []

    for fi, faces in sample_items:
        # lip motion relative to the nearest face in the previous sample
        for f in faces:
            near = min(prev_faces, key=lambda p: abs(p['cx'] - f['cx']), default=None)
            f['motion'] = (abs(f['lip'] - near['lip'])
                           if near and abs(near['cx'] - f['cx']) < _SAME_FACE_DX else 0.0)
        prev_faces = faces or prev_faces

        if not faces:
            out.append((fi, cur, False))
            continue

        speaking = fi in speech_frames
        best = max(faces, key=lambda f: f['h'] * f['w'] *
                   ((1.0 + 8.0 * f['motion'] + 2.0 * f['lip']) if speaking else 1.0))
        if cur is None:
            cur = best
            out.append((fi, cur, False))
            continue

        same = min(faces, key=lambda f: abs(f['cx'] - cur['cx']))
        if abs(same['cx'] - cur['cx']) >= _SAME_FACE_DX:
            same = None
        switched = False
        if same is not None and same is best:
            cur, pending_since = same, None
        else:
            if pending_since is None:
                pending_since = fi
            if fi - pending_since >= hold or same is None and fi - pending_since >= hold // 2:
                cur, pending_since, switched = best, None, True
            elif same is not None:
                cur = same
        out.append((fi, cur, switched))

    first = next((c for _, c, _ in out if c is not None), None)
    if tracked is not None:
        tracked.extend(c for _, c, _ in out if c is not None)
    return [(fi, (c or first).get(xkey, (c or first)['cx']) if (c or first) else 0.5,
             (c or first).get(key, (c or first)['cy']) if (c or first) else 0.45, sw)
            for fi, c, sw in out]


def _crop_path(track, shot_start, shot_end, src_w, src_h):
    """Per-frame (cx, cy) in source pixels: interpolate, light EMA, snap on switches."""
    frames = list(range(shot_start, shot_end))
    if not track:
        return [(src_w / 2.0, src_h / 2.0)] * len(frames)
    path = []
    k = 0
    pos = None
    dz = _DEAD_ZONE * src_w
    for f in frames:
        while k + 1 < len(track) and track[k + 1][0] <= f:
            k += 1
        fi, cx, cy, sw = track[k]
        snap = pos is None or (sw and f == fi)
        if k + 1 < len(track) and f >= fi and not track[k + 1][3]:
            nf, ncx, ncy, _ = track[k + 1]
            t = (f - fi) / max(1, nf - fi)
            cx, cy = cx + (ncx - cx) * t, cy + (ncy - cy) * t
        tx, ty = cx * src_w, cy * src_h
        if snap:
            pos = [tx, ty]
        else:
            if abs(tx - pos[0]) > dz:
                pos[0] += _SMOOTH_ALPHA * (tx - pos[0])
            if abs(ty - pos[1]) > dz:
                pos[1] += _SMOOTH_ALPHA * (ty - pos[1])
        path.append((pos[0], pos[1]))
    return path


def _warp(frame, x1, y1, cw, ch, out_w, out_h):
    """Sub-pixel crop [x1, x1+cw) x [y1, y1+ch) scaled to out_w x out_h."""
    sx, sy = out_w / cw, out_h / ch
    m = np.float32([[sx, 0, -x1 * sx], [0, sy, -y1 * sy]])
    return cv2.warpAffine(frame, m, (out_w, out_h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def _face_crop(frame, cx, cy, cw, ch, src_w, src_h, out_w, out_h):
    """Crop of size cw x ch with the face slightly above centre, clamped to the frame."""
    x1 = max(0.0, min(cx - cw / 2, src_w - cw))
    y1 = max(0.0, min(cy - ch * 0.42, src_h - ch))
    return _warp(frame, x1, y1, cw, ch, out_w, out_h)


def _blur_fill(frame, out_w, out_h):
    """Whole wide frame fitted to width over a blurred, darkened cover of itself."""
    h, w = frame.shape[:2]
    scale = max(out_w / w, out_h / h)
    small = cv2.resize(frame, (max(1, int(w * scale / 10)), max(1, int(h * scale / 10))),
                       interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), 4)
    # never smaller than the output: int() truncation left a 1-px-short cover and an empty slice
    bw, bh = max(out_w, int(np.ceil(w * scale))), max(out_h, int(np.ceil(h * scale)))
    bg = cv2.resize(small, (bw, bh), interpolation=cv2.INTER_LINEAR)
    oy, ox = (bg.shape[0] - out_h) // 2, (bg.shape[1] - out_w) // 2
    bg = (bg[oy:oy + out_h, ox:ox + out_w] * 0.55).astype(np.uint8)
    fg_h = min(out_h, int(round(out_w * h / w)))
    fg = cv2.resize(frame, (out_w, fg_h), interpolation=cv2.INTER_AREA)
    y = (out_h - fg_h) // 2
    bg[y:y + fg_h] = fg
    return bg


def _bg_cover(frame):
    """A1 background: the frame's 9:16 centre, heavily blurred at 1/8 scale, dimmed."""
    h, w = frame.shape[:2]
    if w / h > _OUT_W / _OUT_H:
        cw = int(h * _OUT_W / _OUT_H)
        x = (w - cw) // 2
        frame = frame[:, x:x + cw]
    else:
        ch = int(w * _OUT_H / _OUT_W)
        y = (h - ch) // 2
        frame = frame[y:y + ch]
    small = cv2.resize(frame, (_OUT_W // 8, _OUT_H // 8), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), 5)
    return cv2.convertScaleAbs(cv2.resize(small, (_OUT_W, _OUT_H), interpolation=cv2.INTER_LINEAR),
                               alpha=_BG_DIM)


def _pair(faces):
    """Two biggest faces of similar size (each >= _SPLIT_MIN_REL of the other), or None."""
    if len(faces) < 2:
        return None
    a, b = sorted(faces, key=lambda f: f['h'], reverse=True)[:2]
    return (a, b) if b['h'] >= a['h'] * _SPLIT_MIN_REL else None


def _eye_rect(cx, ey, cw, ch, src_w, src_h, eye_frac, top=None):
    """Top-left of a cw x ch crop centred on cx with the eye line at eye_frac of
    its height; the forehead (top, source px) stays _HEAD_TOP_MIN canvas px down."""
    x1 = max(0.0, min(cx - cw / 2, src_w - cw))
    y1 = ey - ch * eye_frac
    if top is not None:
        y1 = min(y1, top - ch * _HEAD_TOP_MIN / _OUT_H)
    y1 = max(0.0, min(y1, src_h - ch))
    return x1, y1


def analyse_clip(video_path, start_sec, end_sec, pipeline=None, wide_fallback=False, detect=True):
    """Face samples, camera cuts, letterbox profiles and (optional) speech frames for one
    part. detect=False skips face detection and diarization (the blurred-fill fallback)."""
    rate, src_fps = probe_fps(video_path)
    cap = cv2.VideoCapture(video_path)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    start_f = int(round(start_sec * src_fps))
    total_frames = max(1, int(round((end_sec - start_sec) * src_fps)))

    speech_frames: set = set()
    if pipeline is not None and detect:
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            wav_tmp = f.name
        try:
            _extract_wav(video_path, start_sec, end_sec, wav_tmp)
            speech_frames = _speech_frames(pipeline, wav_tmp, src_fps)
        except Exception as exc:
            print(f'    Diarization skipped: {exc}')
        finally:
            os.unlink(wav_tmp)

    print('    Detecting faces, lip movement and camera cuts...' if detect else
          '    Detecting camera cuts and letterbox bars...')
    frame_data, cuts, bars = _sample_faces(video_path, start_f, total_frames, src_fps,
                                           tiled=wide_fallback, detect=detect)
    an = {'video': video_path, 'rate': rate, 'fps': src_fps, 'src_w': src_w, 'src_h': src_h,
          'start_f': start_f, 'total_frames': total_frames, 'faces': frame_data,
          'cuts': cuts, 'speech': speech_frames, 'wide_fallback': wide_fallback, 'bars': bars}
    boxed = [box for _, _, box in shot_boxes(an) if box != (0, 0, src_w, src_h)]
    if boxed:
        print(f'    Letterbox: bars cropped off in {len(boxed)} shot(s), picture {boxed[0]}')
    return an


def _base_crop(src_w, src_h, aspect):
    """(cw, ch) of the largest crop of the given aspect (w / h) inside src_w x src_h."""
    if src_w / src_h >= aspect:
        return src_h * aspect, float(src_h)
    return float(src_w), src_w / aspect


def _shot_items(an, a, b, box):
    """(frame, faces) samples of shot [a, b) with faces in the content box's coordinates."""
    return [(fi, _local_faces(an['faces'][fi], box, an['src_w'], an['src_h']))
            for fi in sorted(an['faces']) if a <= fi < b]


def _plan_legacy(an, top_reserved, boxes=None):
    """Today's per-frame plan: full-width crop below the banner / split / wide.
    Entries are in the shot's content-box px (boxes: shot_boxes(); None = full frame)."""
    total = an['total_frames']
    content_h = _OUT_H - top_reserved
    aspect = _OUT_W / content_h
    full = (0, 0, an['src_w'], an['src_h'])
    boxes = boxes or [(a, b, full) for a, b in _shots(an['cuts'], total)]
    plan = [None] * total
    first_cw = None
    for a, b, box in boxes:
        src_w, src_h = box[2] - box[0], box[3] - box[1]
        base_cw, base_ch = _base_crop(src_w, src_h, aspect)
        first_cw = first_cw or base_cw
        items = _shot_items(an, a, b, box)
        layout = _shot_layout([f for _, f in items], an['wide_fallback'], base_cw / src_w)
        if layout == 'split':
            pairs = [p for p in (_split_pair(f, base_cw / src_w) for _, f in items) if p]
            left = {k: float(np.median([p[0][k] for p in pairs])) for k in ('cx', 'cy', 'h')}
            right = {k: float(np.median([p[1][k] for p in pairs])) for k in ('cx', 'cy', 'h')}
            for f in range(a, b):
                plan[f] = ('split', left, right)
        elif layout == 'wide':
            for f in range(a, b):
                plan[f] = ('wide',)
        else:
            track = _track_shot(items, an['speech'], an['fps'])
            for f, (cx, cy) in zip(range(a, b), _crop_path(track, a, b, src_w, src_h)):
                plan[f] = ('crop', cx, cy, base_cw, base_ch)
    return plan, first_cw, content_h


def _plan_template(an, layout, boxes=None):
    """
    Per-frame plan for the A1 / A2 / B1 templates.

    Entries: ('win', cx, ey, zoom, win_y, win_h, cw, ch, top) — a face-placed
    crop of cw x ch source px (before zoom) rendered into rows win_y..win_y+win_h;
    or ('split', l, r) / ('wide',) as in the legacy plan (full-bleed only).
    cx / ey / top in the shot's content-box px (boxes: shot_boxes(); None = the
    full frame); ey None = no face, centre the crop.
    """
    total = an['total_frames']
    if layout == 'A1':
        win_y, win_h = A1_WINDOW
    elif layout == 'B1H':
        win_y, win_h = B1H_WINDOW
    else:
        win_y, win_h = 0, _OUT_H
    aspect = _OUT_W / win_h
    full = (0, 0, an['src_w'], an['src_h'])
    boxes = boxes or [(a, b, full) for a, b in _shots(an['cuts'], total)]

    plan = [None] * total
    first_cw = None
    for a, b, box in boxes:
        src_w, src_h = box[2] - box[0], box[3] - box[1]
        base_cw, base_ch = _base_crop(src_w, src_h, aspect)
        first_cw = first_cw or base_cw
        items = _shot_items(an, a, b, box)
        samples = [f for _, f in items]

        if layout != 'A1':
            shot = _shot_layout(samples, an['wide_fallback'], base_cw / src_w)
            if shot == 'split':
                pairs = [p for p in (_split_pair(f, base_cw / src_w) for f in samples) if p]
                left = {k: float(np.median([p[0][k] for p in pairs])) for k in ('cx', 'cy', 'h')}
                right = {k: float(np.median([p[1][k] for p in pairs])) for k in ('cx', 'cy', 'h')}
                for f in range(a, b):
                    plan[f] = ('split', left, right)
                continue
            if shot == 'wide':
                for f in range(a, b):
                    plan[f] = ('wide',)
                continue
        else:
            # A1 two-shot: widen the window until both faces fit (4:5 -> 1:1 -> full width)
            pairs = [p for p in (_pair(f) for f in samples) if p]
            if samples and len(pairs) * 2 >= len(samples):
                x0 = float(np.median([min(p[0]['x0'], p[1]['x0']) for p in pairs])) * src_w
                x1 = float(np.median([max(p[0]['x1'], p[1]['x1']) for p in pairs])) * src_w
                need = (x1 - x0) * 1.25                       # breathing room either side
                if need <= base_cw:
                    cw, ch, wy, wh = base_cw, base_ch, win_y, win_h
                elif need <= min(src_h, src_w):
                    cw = ch = float(min(src_h, src_w))
                    wy, wh = A1_SQUARE
                else:
                    cw, ch = float(src_w), float(src_h)
                    wh = int(round(_OUT_W * src_h / src_w / 2)) * 2
                    wy = A1_BAND_MID - wh // 2
                for f in range(a, b):
                    plan[f] = ('win', (x0 + x1) / 2, None, 1.0, wy, wh, cw, ch, None)
                continue

        tracked = []
        # templates centre the face box (the nose tip sits off-centre on a turned head)
        track = _track_shot(items, an['speech'], an['fps'], key='ey', tracked=tracked, xkey='bx')
        if tracked:
            fh = float(np.median([t['h'] for t in tracked])) * src_h       # face height, source px
            dtop = float(np.median([t['ey'] - t['top'] for t in tracked])) * src_h
            frac = fh / base_ch * (win_h / _OUT_H)                         # canvas share at zoom 1
            target, cap = ((_A1_FACE_TARGET, _A1_ZOOM_MAX) if layout == 'A1'
                           else (_FB_FACE_TARGET, _FB_ZOOM_MAX))
            z = min(cap, max(1.0, target / frac)) if frac > 0 else 1.0
            for f, (cx, ey) in zip(range(a, b), _crop_path(track, a, b, src_w, src_h)):
                plan[f] = ('win', cx, ey, z, win_y, win_h, base_cw, base_ch, ey - dtop)
        else:
            for f in range(a, b):
                plan[f] = ('win', src_w / 2.0, None, 1.0, win_y, win_h, base_cw, base_ch, None)
    return plan, first_cw


def reframe_clip(video_path: str, start_sec: float, end_sec: float,
                 output_path: str, pipeline=None, wide_fallback: bool = False,
                 top_reserved: int = 0, zoom: float = 1.0, push_in: bool = False,
                 layout: str = 'legacy', analysis=None, zoom_events=None, push=None,
                 force_wide: bool = False, flash=None, zoom_max=None):
    """
    Produce a video-only 1080x1920 clip of round((end-start)*fps) frames.

    layout 'legacy': the picture fills the whole area below top_reserved px
    (the hook banner area, left black for the banner to cover) with a
    face-tracked crop. 'A1' / 'A2' / 'B1': see the module docstring
    (top_reserved is ignored).

    wide_fallback: podcast-style profiles. Shots with two separated faces become
    a stacked split (left face on top, right face below); shots where no face is
    found show the whole frame over a blurred fill instead of a blind crop.
    zoom: punch-in factor (1.0 / 1.12) — clipper alternates it on jump cuts.
    push_in: add a slow _PUSH_IN zoom across the part.
    analysis: analyse_clip() result for this part (skips a second detection pass).
    zoom_events: [(frame, zoom)] punch-in level from that frame on (replaces zoom).
    push: (start_frame, amount) ease-out push-in reaching 1+amount on the last frame.
    force_wide: the QA layout fallback — every frame is the whole picture over
    a blurred fill, with bars cropped by the looser _BAR_AGREE_LOOSE rule.
    flash: {frame: opacity} of the edit layer's light wash (FLASH_RGB) on this part.
    zoom_max: cap on the face zoom x reframe level (edit layer): a part with reframe
    cuts eases its wide framing so the close one stays under it.

    Every shot's letterbox / pillarbox bars are cropped off first (shot_boxes).

    Returns {'faces': [(t, x0, y0, x1, y1)] canvas boxes of detected faces,
    'crop_w': source width of the unzoomed crop, 'letterbox': [[t0, t1, x0, y0,
    x1, y1]] shots (part seconds) whose bars were cropped}.
    """
    an = analysis or analyse_clip(video_path, start_sec, end_sec, pipeline, wide_fallback,
                                  detect=not force_wide)
    rate, full_w, full_h = an['rate'], an['src_w'], an['src_h']
    start_f, total_frames, cuts = an['start_f'], an['total_frames'], an['cuts']
    boxes = shot_boxes(an, _BAR_AGREE_LOOSE if force_wide else _BAR_AGREE)
    frame_box = [None] * total_frames
    for a, b, box in boxes:
        frame_box[a:b] = [box] * (b - a)
    letterbox = [[round(a / an['fps'], 2), round(b / an['fps'], 2), *box] for a, b, box in boxes
                 if box != (0, 0, full_w, full_h)]

    if force_wide:
        plan, base_cw = [('wide',)] * total_frames, None
        top_reserved = top_reserved if layout not in ('A1', 'A2', 'B1', 'B1H') else 0
        content_h = _OUT_H - top_reserved
    elif layout in ('A1', 'A2', 'B1', 'B1H'):
        plan, base_cw = _plan_template(an, layout, boxes)
        top_reserved, content_h = 0, _OUT_H
    else:
        plan, base_cw, content_h = _plan_legacy(an, top_reserved, boxes)
    n_split = sum(1 for p in plan if p and p[0] == 'split')
    n_wide = sum(1 for p in plan if p and p[0] == 'wide')
    if cuts or n_split or n_wide:
        print(f'    {len(cuts)} cut(s); split {n_split} / wide {n_wide} of {total_frames} frames')

    # --- Render: pipe raw BGR frames → ffmpeg → libx264 (video only) ---
    ffmpeg_cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo',
        '-s', f'{_OUT_W}x{_OUT_H}', '-pix_fmt', 'bgr24',
        '-r', rate,
        '-i', 'pipe:0',
        '-an',
        # intermediate: re-encoded in the caption pass, so a faster preset
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p',
        output_path,
    ]
    proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)

    # Flash frames: a camera cut in the first/last few frames of a part would
    # show a 2-8 frame sliver of another shot (on frame 0 it is also the
    # thumbnail). Freeze the neighbouring shot over those frames instead —
    # the frame count, and so A/V sync, stays the same.
    head = cuts[0] if cuts and cuts[0] <= _FLASH_FRAMES else 0
    tail = (cuts[-1] if cuts and total_frames - cuts[-1] <= _FLASH_FRAMES
            and cuts[-1] > head else total_frames)
    src_idx = [min(max(fi, head), tail - 1) for fi in range(total_frames)]

    events = sorted(zoom_events or [])
    z_hi = max([lv for _, lv in events] + [1.0])
    canvas = np.zeros((_OUT_H, _OUT_W, 3), dtype=np.uint8)
    # B1H keeps split shots inside the window, under the headline band
    split_y = B1H_WINDOW[0] if layout == 'B1H' else top_reserved
    split_h = _OUT_H - split_y if layout == 'B1H' else content_h
    panel_h = split_h // 2
    flash = flash or {}
    wash = np.array(FLASH_RGB[::-1], dtype=np.float32)          # BGR
    face_boxes = []
    frame, read = None, 0
    for fi in range(total_frames):
        while read <= src_idx[fi]:
            ret, nxt = cap.read()
            read += 1
            if not ret:
                read = total_frames   # EOF: repeat the last frame to keep the count
                break
            frame = nxt
        if frame is None:
            break

        if zoom_events is not None:
            z = next((lvl for f0, lvl in reversed(events) if f0 <= fi), 1.0)
        else:
            z = zoom * (1.0 + (_PUSH_IN * fi / max(1, total_frames - 1) if push_in else 0.0))
        if push and fi >= push[0]:
            pr = (fi - push[0]) / max(1, total_frames - 1 - push[0])
            z *= 1.0 + push[1] * (1.0 - (1.0 - pr) ** 3)
        p = plan[src_idx[fi]]
        box = frame_box[src_idx[fi]] or (0, 0, full_w, full_h)
        # the plan is in content-box px: drop the shot's letterbox bars first
        pic = frame if box == (0, 0, full_w, full_h) else frame[box[1]:box[3], box[0]:box[2]]
        src_w, src_h = box[2] - box[0], box[3] - box[1]
        rect = None                      # (x1, y1, cw, ch, out_y, out_h) of a single crop
        if p[0] == 'wide':
            if z > 1.001:
                # a reframe cut on a no-face wide shot: a tighter centre crop of the same picture
                ph, pw = pic.shape[:2]
                cw_, ch_ = int(round(pw / z)), int(round(ph / z))
                pic = pic[(ph - ch_) // 2:(ph - ch_) // 2 + ch_, (pw - cw_) // 2:(pw - cw_) // 2 + cw_]
            canvas[top_reserved:] = _blur_fill(pic, _OUT_W, content_h)
        elif p[0] == 'split':
            panels = []
            for face in (p[1], p[2]):
                # floor at 35% of the frame height: tiny (tiled) faces would
                # otherwise be upscaled ~6x into a soft, blocky panel
                ch = min(src_h, max(face['h'] * 2.6, _SPLIT_MIN_CROP) * src_h) / z
                cw = ch * _OUT_W / panel_h
                if cw > src_w:
                    cw, ch = float(src_w), src_w * panel_h / _OUT_W
                panels.append(_face_crop(pic, face['cx'] * src_w, face['cy'] * src_h,
                                         cw, ch, src_w, src_h, _OUT_W, panel_h))
            content = np.vstack(panels + ([panels[-1][-1:]] * (split_h - 2 * panel_h)))
            content[panel_h - 2:panel_h + 2] = 0     # thin seam between the faces
            if layout == 'B1H':
                canvas[:split_y] = _bg_cover(pic)[:split_y]
            canvas[split_y:] = content
        elif p[0] == 'crop':
            cw, ch = p[3] / z, p[4] / z
            canvas[top_reserved:] = _face_crop(pic, p[1], p[2], cw, ch, src_w, src_h,
                                               _OUT_W, content_h)
            rect = (max(0.0, min(p[1] - cw / 2, src_w - cw)),
                    max(0.0, min(p[2] - ch * 0.42, src_h - ch)), cw, ch, top_reserved, content_h)
        else:                                        # 'win' (templates)
            _, cx, ey, zf, wy, wh, cw0, ch0, top = p
            if zoom_max and z_hi > 1.0:
                # wide x close stays under zoom_max: ease the wide framing first, never below 1.0
                zf = max(1.0, min(zf, zoom_max / z_hi))
            zz = zf * z if not zoom_max else min(zf * z, max(zoom_max, zf))
            cw, ch = cw0 / zz, ch0 / zz
            if ey is None:
                x1 = max(0.0, min(cx - cw / 2, src_w - cw))
                y1 = max(0.0, (src_h - ch) / 2)
            else:
                x1, y1 = _eye_rect(cx, ey, cw, ch, src_w, src_h,
                                   _A1_EYE if layout == 'A1' else _FB_EYE, top)
            rect = (x1, y1, cw, ch, wy, wh)
            if layout in _WINDOWED:
                canvas[:] = _bg_cover(pic)
                canvas[wy:wy + wh] = _warp(pic, x1, y1, cw, ch, _OUT_W, wh)
            else:
                canvas[:] = _warp(pic, x1, y1, cw, ch, _OUT_W, _OUT_H)
        a = flash.get(fi)
        if a:
            canvas[:] = (canvas.astype(np.float32) * (1.0 - a) + wash * a).astype(np.uint8)
        proc.stdin.write(canvas.tobytes())

        # canvas boxes of the faces sampled on this frame (caption placement)
        faces = an['faces'].get(src_idx[fi]) if rect else None
        if faces:
            faces = _local_faces(faces, box, full_w, full_h)
            x1, y1, cw, ch, oy, oh = rect
            sx, sy = _OUT_W / cw, oh / ch
            for f in faces:
                bx0 = (f.get('x0', f['cx'] - f['w'] / 2) * src_w - x1) * sx
                bx1 = (f.get('x1', f['cx'] + f['w'] / 2) * src_w - x1) * sx
                by0 = (f.get('top', f['cy'] - f['h'] / 2) * src_h - y1) * sy + oy
                by1 = (f.get('bot', f['cy'] + f['h'] / 2) * src_h - y1) * sy + oy
                if bx1 > 0 and bx0 < _OUT_W and by1 > oy and by0 < oy + oh:
                    face_boxes.append((fi / an['fps'], bx0, by0, bx1, by1))

    cap.release()
    try:
        proc.stdin.close()
    except BrokenPipeError:
        pass
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f'reframe_clip ffmpeg failed for {output_path}')
    return {'faces': face_boxes, 'crop_w': base_cw, 'letterbox': letterbox}
