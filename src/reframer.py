"""
Speaker-aware 9:16 reframing.

Pipeline:
  1. pyannote.audio  — who speaks when (optional, needs HF_TOKEN)
  2. MediaPipe FaceMesh — face positions + lip openness per sampled frame
  3. Smooth exponential crop — pans to the speaking face
  4. ffmpeg pipe — renders blurred-bg + sharp fg at 1080x1920
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
_SAMPLE_FPS   = 3      # face-detection samples per second of source video
_SMOOTH_ALPHA = 0.03   # EMA smoothing — lower = slower, steadier pan

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


def _sample_faces(video_path, start_sec, end_sec, src_fps) -> dict:
    """
    Returns {clip_frame_idx: [(nose_cx_norm, lip_openness), ...]}
    Samples at _SAMPLE_FPS using FaceLandmarker (mediapipe Tasks API).
    """
    step    = max(1, round(src_fps / _SAMPLE_FPS))
    start_f = int(start_sec * src_fps)
    end_f   = int(end_sec   * src_fps)

    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)

    landmarker = _create_face_landmarker()
    data = {}
    try:
        for fi in range(end_f - start_f):
            ret, frame = cap.read()
            if not ret:
                break
            if fi % step != 0:
                continue
            rgb      = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result   = landmarker.detect(mp_image)
            faces    = []
            for face_lms in result.face_landmarks:
                cx       = face_lms[_NOSE_TIP].x
                lip_open = abs(face_lms[_LIP_BOT].y - face_lms[_LIP_TOP].y)
                face_h   = abs(face_lms[_FACE_BOT].y - face_lms[_FACE_TOP].y) or 0.1
                faces.append((cx, lip_open / face_h))
            data[fi] = faces
    finally:
        cap.release()
        landmarker.close()
    return data


def _build_cx_timeline(frame_data, speech_frames, total_frames, src_w) -> list:
    """Compute per-frame crop-center x (pixels in source), then interpolate."""
    sampled = sorted(frame_data.keys())
    cx_at   = {}
    last_cx = src_w / 2.0

    for fi in sampled:
        faces = frame_data[fi]
        if not faces:
            cx_at[fi] = last_cx
            continue

        if fi in speech_frames:
            # pick the face with most lip movement
            best = max(faces, key=lambda f: f[1])
        else:
            # no speech signal — keep largest-appearing face (first detected)
            best = faces[0]

        last_cx   = best[0] * src_w
        cx_at[fi] = last_cx

    # Linear interpolation to all frames
    result = []
    for i in range(total_frames):
        if not sampled or i <= sampled[0]:
            result.append(cx_at.get(sampled[0] if sampled else 0, src_w / 2))
        elif i >= sampled[-1]:
            result.append(cx_at[sampled[-1]])
        else:
            lo = max(f for f in sampled if f <= i)
            hi = min(f for f in sampled if f >= i)
            t  = (i - lo) / (hi - lo) if hi != lo else 0
            result.append(cx_at[lo] * (1 - t) + cx_at[hi] * t)
    return result


def _smooth(values, alpha):
    out = list(values)
    for i in range(1, len(out)):
        out[i] = alpha * out[i] + (1 - alpha) * out[i - 1]
    return out


def reframe_clip(video_path: str, start_sec: float, end_sec: float,
                 output_path: str, pipeline=None) -> None:
    """
    Produce a 1080x1920 vertical clip: black letterbox top/bottom, face-tracked
    crop filling 70% of output height at full output width.
    """
    cap = cv2.VideoCapture(video_path)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    src_w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    start_f      = int(start_sec * src_fps)
    total_frames = int((end_sec - start_sec) * src_fps)
    crop_w       = min(int(src_h * 9 / 16), src_w)  # used for face detection

    # Foreground: face-tracked crop fills 70% of output height at full output width.
    # Derive crop width from desired display height to avoid side bands or distortion.
    fg_disp_h   = int(_OUT_H * 0.70)
    crop_w_disp = min(int(_OUT_W * src_h / fg_disp_h), src_w)
    if crop_w_disp == src_w:          # very wide source — clamp and recalc height
        fg_disp_h = int(_OUT_W * src_h / src_w)
    fg_y     = (_OUT_H - fg_disp_h) // 2
    black_bg = np.zeros((_OUT_H, _OUT_W, 3), dtype=np.uint8)

    # --- Speaker diarization (optional) ---
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

    # --- Face sampling ---
    print('    Detecting faces + lip movement...')
    frame_data = _sample_faces(video_path, start_sec, end_sec, src_fps)

    # --- Crop trajectory ---
    crop_cx = _build_cx_timeline(frame_data, speech_frames, total_frames, src_w)
    crop_cx = _smooth(crop_cx, _SMOOTH_ALPHA)
    half    = crop_w_disp / 2
    crop_cx = [max(half, min(src_w - half, cx)) for cx in crop_cx]

    # --- Render: pipe raw BGR frames → ffmpeg → libx264 ---
    ffmpeg_cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo',
        '-s', f'{_OUT_W}x{_OUT_H}', '-pix_fmt', 'bgr24',
        '-r', str(src_fps),
        '-i', 'pipe:0',
        '-ss', str(start_sec), '-to', str(end_sec), '-i', video_path,
        '-map', '0:v', '-map', '1:a?',
        '-c:v', 'libx264', '-preset', 'slow', '-crf', '18',
        '-c:a', 'aac', '-b:a', '128k',
        '-shortest', output_path,
    ]
    proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)

    for fi in range(total_frames):
        ret, frame = cap.read()
        if not ret:
            break

        cx = int(crop_cx[fi])
        x1 = max(0, min(cx - crop_w_disp // 2, src_w - crop_w_disp))

        # Black canvas with face-tracked crop centered vertically
        fg     = cv2.resize(frame[:, x1:x1 + crop_w_disp], (_OUT_W, fg_disp_h),
                            interpolation=cv2.INTER_LANCZOS4)
        canvas = black_bg.copy()
        canvas[fg_y:fg_y + fg_disp_h, :] = fg
        proc.stdin.write(canvas.tobytes())

    cap.release()
    try:
        proc.stdin.close()
    except BrokenPipeError:
        pass
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f'reframe_clip ffmpeg failed for {output_path}')
