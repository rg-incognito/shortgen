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
Frames are never letterboxed with black bars.
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

# Stacked split (wide_fallback profiles): two faces each at least this large
# relative to the bigger one, too far apart to share one crop.
_SPLIT_MIN_REL = 0.55
_SPLIT_MIN_CROP = 0.35   # split panel crop height, min fraction of the frame

# Templates. A1: 4:5 window over a blurred cover of the frame.
A1_WINDOW = (300, 1350)        # y, height of the default 4:5 window
A1_SQUARE = (420, 1080)        # two-shot window (1:1)
A1_BAND_MID = 975              # centre of the window band (300..1650)
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


def _sample_faces(video_path, start_f, total_frames, src_fps, tiled=False):
    """
    Reads every frame of the clip once.

    Returns (faces, cuts):
      faces: {clip_frame_idx: [face, ...]} sampled at _SAMPLE_FPS plus the first
             frame after every cut; face = dict(cx, cy, h, w, lip) normalised 0-1
      cuts:  sorted clip frame indices where a new camera shot starts
    """
    step = max(1, round(src_fps / _SAMPLE_FPS))

    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)

    landmarker = _create_face_landmarker()
    data, cuts = {}, []
    prev_thumb = None
    last_sample = -step
    try:
        for fi in range(total_frames):
            ret, frame = cap.read()
            if not ret:
                break
            thumb = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 36),
                               interpolation=cv2.INTER_AREA).astype(np.int16)
            is_cut = (prev_thumb is not None
                      and float(np.mean(np.abs(thumb - prev_thumb))) > _CUT_MAD)
            prev_thumb = thumb
            if is_cut:
                cuts.append(fi)
            if not (is_cut or fi - last_sample >= step):
                continue
            last_sample = fi
            rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            faces = _detect(landmarker, rgb)
            if not faces and tiled:
                faces = _detect_tiled(landmarker, rgb)
            data[fi] = faces
    finally:
        cap.release()
        landmarker.close()
    return data, cuts


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
    bg = cv2.resize(small, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LINEAR)
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


def analyse_clip(video_path, start_sec, end_sec, pipeline=None, wide_fallback=False):
    """Face samples, camera cuts and (optional) speech frames for one part."""
    rate, src_fps = probe_fps(video_path)
    cap = cv2.VideoCapture(video_path)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    start_f = int(round(start_sec * src_fps))
    total_frames = max(1, int(round((end_sec - start_sec) * src_fps)))

    speech_frames: set = set()
    if pipeline is not None:
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            wav_tmp = f.name
        try:
            _extract_wav(video_path, start_sec, end_sec, wav_tmp)
            speech_frames = _speech_frames(pipeline, wav_tmp, src_fps)
        except Exception as exc:
            print(f'    Diarization skipped: {exc}')
        finally:
            os.unlink(wav_tmp)

    print('    Detecting faces, lip movement and camera cuts...')
    frame_data, cuts = _sample_faces(video_path, start_f, total_frames, src_fps,
                                     tiled=wide_fallback)
    return {'video': video_path, 'rate': rate, 'fps': src_fps, 'src_w': src_w, 'src_h': src_h,
            'start_f': start_f, 'total_frames': total_frames, 'faces': frame_data,
            'cuts': cuts, 'speech': speech_frames, 'wide_fallback': wide_fallback}


def _plan_legacy(an, top_reserved):
    """Today's per-frame plan: full-width crop below the banner / split / wide."""
    src_w, src_h, total = an['src_w'], an['src_h'], an['total_frames']
    content_h = _OUT_H - top_reserved
    aspect = _OUT_W / content_h
    if src_w / src_h >= aspect:
        base_ch, base_cw = float(src_h), src_h * aspect
    else:
        base_cw, base_ch = float(src_w), src_w / aspect
    plan = [None] * total
    for a, b in _shots(an['cuts'], total):
        items = [(fi, an['faces'][fi]) for fi in sorted(an['faces']) if a <= fi < b]
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
                plan[f] = ('crop', cx, cy)
    return plan, base_cw, base_ch, content_h


def _plan_template(an, layout):
    """
    Per-frame plan for the A1 / A2 / B1 templates.

    Entries: ('win', cx, ey, zoom, win_y, win_h, cw, ch, top) — a face-placed
    crop of cw x ch source px (before zoom) rendered into rows win_y..win_y+win_h;
    or ('split', l, r) / ('wide',) as in the legacy plan (full-bleed only).
    cx / ey / top in source px; ey None = no face, centre the crop.
    """
    src_w, src_h, total = an['src_w'], an['src_h'], an['total_frames']
    if layout == 'A1':
        win_y, win_h = A1_WINDOW
    else:
        win_y, win_h = 0, _OUT_H
    aspect = _OUT_W / win_h
    if src_w / src_h >= aspect:
        base_ch, base_cw = float(src_h), src_h * aspect
    else:
        base_cw, base_ch = float(src_w), src_w / aspect

    plan = [None] * total
    for a, b in _shots(an['cuts'], total):
        items = [(fi, an['faces'][fi]) for fi in sorted(an['faces']) if a <= fi < b]
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
    return plan, base_cw, base_ch


def reframe_clip(video_path: str, start_sec: float, end_sec: float,
                 output_path: str, pipeline=None, wide_fallback: bool = False,
                 top_reserved: int = 0, zoom: float = 1.0, push_in: bool = False,
                 layout: str = 'legacy', analysis=None, zoom_events=None, push=None):
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

    Returns {'faces': [(t, x0, y0, x1, y1)] canvas boxes of detected faces,
    'crop_w': source width of the unzoomed crop}.
    """
    an = analysis or analyse_clip(video_path, start_sec, end_sec, pipeline, wide_fallback)
    rate, src_w, src_h = an['rate'], an['src_w'], an['src_h']
    start_f, total_frames, cuts = an['start_f'], an['total_frames'], an['cuts']

    if layout in ('A1', 'A2', 'B1'):
        plan, base_cw, base_ch = _plan_template(an, layout)
        top_reserved, content_h = 0, _OUT_H
    else:
        plan, base_cw, base_ch, content_h = _plan_legacy(an, top_reserved)
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
    canvas = np.zeros((_OUT_H, _OUT_W, 3), dtype=np.uint8)
    panel_h = content_h // 2
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
        rect = None                      # (x1, y1, cw, ch, out_y, out_h) of a single crop
        if p[0] == 'wide':
            canvas[top_reserved:] = _blur_fill(frame, _OUT_W, content_h)
        elif p[0] == 'split':
            panels = []
            for face in (p[1], p[2]):
                # floor at 35% of the frame height: tiny (tiled) faces would
                # otherwise be upscaled ~6x into a soft, blocky panel
                ch = min(src_h, max(face['h'] * 2.6, _SPLIT_MIN_CROP) * src_h) / z
                cw = ch * _OUT_W / panel_h
                if cw > src_w:
                    cw, ch = float(src_w), src_w * panel_h / _OUT_W
                panels.append(_face_crop(frame, face['cx'] * src_w, face['cy'] * src_h,
                                         cw, ch, src_w, src_h, _OUT_W, panel_h))
            content = np.vstack(panels + ([panels[-1][-1:]] * (content_h - 2 * panel_h)))
            content[panel_h - 2:panel_h + 2] = 0     # thin seam between the faces
            canvas[top_reserved:] = content
        elif p[0] == 'crop':
            cw, ch = base_cw / z, base_ch / z
            canvas[top_reserved:] = _face_crop(frame, p[1], p[2], cw, ch, src_w, src_h,
                                               _OUT_W, content_h)
            rect = (max(0.0, min(p[1] - cw / 2, src_w - cw)),
                    max(0.0, min(p[2] - ch * 0.42, src_h - ch)), cw, ch, top_reserved, content_h)
        else:                                        # 'win' (templates)
            _, cx, ey, zf, wy, wh, cw0, ch0, top = p
            cw, ch = cw0 / (zf * z), ch0 / (zf * z)
            if ey is None:
                x1 = max(0.0, min(cx - cw / 2, src_w - cw))
                y1 = max(0.0, (src_h - ch) / 2)
            else:
                x1, y1 = _eye_rect(cx, ey, cw, ch, src_w, src_h,
                                   _A1_EYE if layout == 'A1' else _FB_EYE, top)
            rect = (x1, y1, cw, ch, wy, wh)
            if layout == 'A1':
                canvas[:] = _bg_cover(frame)
                canvas[wy:wy + wh] = _warp(frame, x1, y1, cw, ch, _OUT_W, wh)
            else:
                canvas[:] = _warp(frame, x1, y1, cw, ch, _OUT_W, _OUT_H)
        proc.stdin.write(canvas.tobytes())

        # canvas boxes of the faces sampled on this frame (caption placement)
        faces = an['faces'].get(src_idx[fi]) if rect else None
        if faces:
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
    return {'faces': face_boxes, 'crop_w': base_cw}
