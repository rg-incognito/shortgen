"""
Local test runner — same editing pipeline as GitHub Actions.

Usage:
    python local_test/run.py                   # picks first video in local_test/input/
    python local_test/run.py path/to/video.mp4 # use a specific file

Requirements:
    pip install -r requirements.txt
    Set GEMINI_API_KEY in your environment (or paste it when prompted).

Output lands in local_test/output/.
"""
import os
import sys
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'src'))

# ── Import src modules (same as GitHub pipeline) ──────────────────────────────
from transcriber import transcribe_video
from analyzer import analyze_transcript, generate_yt_metadata
import clipper as _clipper_mod
from clipper import create_story_short

import subprocess as _sp

# ── Windows Fix 1: merge ──────────────────────────────────────────────────────
# ffmpeg concat demuxer fails to parse Windows absolute paths in list files.
# Use multiple -i inputs with filter_complex instead.

def _probe_duration(path):
    """Return duration in seconds, or 0.0 if the file is empty/invalid."""
    r = _sp.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _win_merge_clips(clip_paths, output_path):
    # Drop parts that are empty or shorter than one frame (~0.04 s).
    valid = [(i, p) for i, p in enumerate(clip_paths)
             if _probe_duration(p) >= 0.04]
    if not valid:
        raise RuntimeError('All parts are empty — story clips likely have zero duration')
    skipped = len(clip_paths) - len(valid)
    if skipped:
        print(f'  [merge] skipped {skipped} empty part(s); merging {len(valid)} valid parts')

    n = len(valid)
    inputs = []
    for _, p in valid:
        inputs += ['-i', p]
    streams = ''.join(f'[{i}:v][{i}:a]' for i in range(n))
    filter_complex = f'{streams}concat=n={n}:v=1:a=1[v][a]'
    try:
        _sp.run([
            'ffmpeg', '-y', *inputs,
            '-filter_complex', filter_complex,
            '-map', '[v]', '-map', '[a]',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
            '-c:a', 'aac', '-b:a', '128k',
            output_path,
        ], check=True, capture_output=True)
    except _sp.CalledProcessError as e:
        print(e.stderr.decode('utf-8', errors='replace')[-2000:])
        raise

_clipper_mod._merge_clips = _win_merge_clips

# ── Windows Fix 2: cv2 frame-based seeking ────────────────────────────────────
# cv2.VideoCapture.set(CAP_PROP_POS_FRAMES, n) silently fails on Windows for
# many codecs, returning 0 frames to the ffmpeg pipe and producing empty MP4s.
# Replace both _sample_faces and reframe_clip with time-based seeking
# (CAP_PROP_POS_MSEC).  Everything else is identical to src/reframer.py.

import cv2 as _cv2
import numpy as _np
import reframer as _rm          # imports the real module; we patch two functions


def _fixed_sample_faces(video_path, start_sec, end_sec, src_fps):
    step  = max(1, round(src_fps / _rm._SAMPLE_FPS))
    end_f = int(end_sec * src_fps) - int(start_sec * src_fps)

    cap = _cv2.VideoCapture(video_path)
    cap.set(_cv2.CAP_PROP_POS_MSEC, start_sec * 1000)   # ← Windows fix

    import mediapipe as _mp
    landmarker = _rm._create_face_landmarker()
    data = {}
    try:
        for fi in range(end_f):
            ret, frame = cap.read()
            if not ret:
                break
            if fi % step != 0:
                continue
            rgb      = _cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB)
            mp_image = _mp.Image(image_format=_mp.ImageFormat.SRGB, data=rgb)
            result   = landmarker.detect(mp_image)
            faces    = []
            for face_lms in result.face_landmarks:
                cx       = face_lms[_rm._NOSE_TIP].x
                lip_open = abs(face_lms[_rm._LIP_BOT].y - face_lms[_rm._LIP_TOP].y)
                face_h   = abs(face_lms[_rm._FACE_BOT].y - face_lms[_rm._FACE_TOP].y) or 0.1
                faces.append((cx, lip_open / face_h))
            data[fi] = faces
    finally:
        cap.release()
        landmarker.close()
    return data


def _fixed_reframe_clip(video_path, start_sec, end_sec, output_path, pipeline=None, **_ignored):
    # Newer reframe_clip keywords (wide_fallback, top_reserved, zoom, layout, ...) are
    # accepted and ignored: this Windows fallback keeps the plain centre-crop path.
    cap     = _cv2.VideoCapture(video_path)
    src_fps = cap.get(_cv2.CAP_PROP_FPS) or 25.0
    src_w   = int(cap.get(_cv2.CAP_PROP_FRAME_WIDTH))
    src_h   = int(cap.get(_cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    _OUT_W, _OUT_H = _rm._OUT_W, _rm._OUT_H
    total_frames   = int((end_sec - start_sec) * src_fps)
    crop_w         = min(int(src_h * 9 / 16), src_w)  # for face detection clamping

    # Content fills 70% of output height, full output width — no side bands, no distortion.
    # Derive crop width from desired display height to keep source aspect ratio.
    fg_disp_h    = int(_OUT_H * 0.70)
    crop_w_disp  = min(int(_OUT_W * src_h / fg_disp_h), src_w)
    if crop_w_disp == src_w:           # source wider than target ratio — clamp
        fg_disp_h = int(_OUT_W * src_h / src_w)
    fg_y = (_OUT_H - fg_disp_h) // 2

    black_bg = _np.zeros((_OUT_H, _OUT_W, 3), dtype=_np.uint8)

    speech_frames: set = set()
    if pipeline is not None:
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            wav_tmp = f.name
        try:
            _rm._extract_wav(video_path, start_sec, end_sec, wav_tmp)
            speech_frames = _rm._speech_frames(pipeline, wav_tmp, src_fps)
        except Exception as exc:
            print(f'    Diarization skipped: {exc}')
        finally:
            os.unlink(wav_tmp)

    print('    Detecting faces + lip movement...')
    frame_data = _rm._sample_faces(video_path, start_sec, end_sec, src_fps)

    crop_cx = _rm._build_cx_timeline(frame_data, speech_frames, total_frames, src_w)
    crop_cx = _rm._smooth(crop_cx, _rm._SMOOTH_ALPHA)
    half    = crop_w_disp / 2
    crop_cx = [max(half, min(src_w - half, cx)) for cx in crop_cx]

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
    proc = _sp.Popen(ffmpeg_cmd, stdin=_sp.PIPE, stderr=_sp.DEVNULL)

    cap = _cv2.VideoCapture(video_path)
    cap.set(_cv2.CAP_PROP_POS_MSEC, start_sec * 1000)   # ← fix

    for fi in range(total_frames):
        ret, frame = cap.read()
        if not ret:
            break
        cx = int(crop_cx[fi])
        x1 = max(0, min(cx - crop_w_disp // 2, src_w - crop_w_disp))

        # Black background, face-tracked crop scaled to fill 70% height at full width
        fg     = _cv2.resize(frame[:, x1:x1 + crop_w_disp], (_OUT_W, fg_disp_h),
                             interpolation=_cv2.INTER_LANCZOS4)
        canvas = black_bg.copy()
        canvas[fg_y:fg_y + fg_disp_h, :] = fg
        proc.stdin.write(canvas.tobytes())

    cap.release()
    try:
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f'reframe_clip ffmpeg failed for {output_path}')


# Patch the real module's functions so clipper's lazy import gets our versions.
# Only where the real signatures still match these fallbacks: the template layouts
# (A1/A2/B1, layout= / analysis=) go to the real reframe_clip.
import inspect as _inspect
if 'start_sec' in _inspect.signature(_rm._sample_faces).parameters:
    _rm._sample_faces = _fixed_sample_faces
_orig_reframe_clip = _rm.reframe_clip


def _reframe_dispatch(*args, **kw):
    if kw.get('layout', 'legacy') != 'legacy' or kw.get('analysis') is not None:
        return _orig_reframe_clip(*args, **kw)
    return _fixed_reframe_clip(*args, **kw)


_rm.reframe_clip = _reframe_dispatch

# ── Windows Fix 3: ASS subtitle path in captioner ────────────────────────────
# ffmpeg's ass= filter uses ':' as option separator, so 'C:/path/file.ass'
# is misread as filter option 'C' + '/path/file.ass'.
# Fix: escape the drive-letter colon as '\:' in the filter string.
import captioner as _captioner_mod
import subprocess as _sub, os as _os, tempfile as _tf2

def _fixed_burn_captions(video_path, output_path, all_words, story_clips, yt_title=None):
    mapped = _captioner_mod._remap_words(all_words, story_clips)
    if not mapped:
        print("  Captions: no words mapped — copying without captions")
        import shutil
        shutil.copy2(video_path, output_path)
        return

    print(f"  Captions: {len(mapped)} words across {len(story_clips)} clips")

    probe = _sub.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
        capture_output=True, text=True
    )
    total_duration = float(probe.stdout.strip()) if probe.stdout.strip() else 60.0

    ass_content = _captioner_mod._build_ass(mapped, total_duration, yt_title=yt_title)
    ass_file = _tf2.mktemp(suffix='.ass')
    try:
        with open(ass_file, 'w', encoding='utf-8') as f:
            f.write(ass_content)

        # Use a relative path so the drive letter 'C:' never appears in the
        # ass= filter value (ffmpeg splits on ':' as an option separator).
        ass_safe = _os.path.relpath(ass_file).replace('\\', '/')
        enhance  = "unsharp=5:5:0.8:3:3:0,eq=contrast=1.05:saturation=1.1"

        cmd = [
            'ffmpeg', '-y',
            '-i', video_path,
            '-vf', f"ass={ass_safe},{enhance}",
            '-map', '0:v', '-map', '0:a?',
            '-c:v', 'libx264', '-preset', 'slow', '-crf', '18',
            '-c:a', 'aac', '-b:a', '128k',
            output_path,
        ]
        try:
            _sub.run(cmd, check=True, capture_output=True)
        except _sub.CalledProcessError as e:
            print(e.stderr.decode('utf-8', errors='replace')[-2000:])
            raise
    finally:
        try:
            _os.unlink(ass_file)
        except OSError:
            pass

# The current captioner (render=, overlays=) writes captions.ass into its work dir and
# runs ffmpeg there (cwd=) with a relative ass= path, so it needs no Windows fix; only
# the legacy captioner is patched.
if 'render' not in _inspect.signature(_captioner_mod.burn_captions).parameters:
    _captioner_mod.burn_captions = _fixed_burn_captions

# ── Directories ───────────────────────────────────────────────────────────────
INPUT_DIR  = Path(__file__).parent / 'input'
OUTPUT_DIR = Path(__file__).parent / 'output'
CACHE_DIR  = Path(__file__).parent / 'cache'
OUTPUT_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)


def main():
    whisper_model = os.environ.get('WHISPER_MODEL', 'small')

    # --- Pick video ---
    if len(sys.argv) > 1:
        video_path = Path(sys.argv[1])
    else:
        videos = sorted(
            list(INPUT_DIR.glob('*.mp4')) +
            list(INPUT_DIR.glob('*.mkv')) +
            list(INPUT_DIR.glob('*.avi'))
        )
        if not videos:
            print(f'No video found in {INPUT_DIR}')
            print('Put a video file there and re-run.')
            sys.exit(1)
        video_path = videos[0]

    if not video_path.exists():
        print(f'Video not found: {video_path}')
        sys.exit(1)

    episode_name     = video_path.stem.replace(' ', '_')
    transcript_cache = CACHE_DIR / f'{episode_name}_transcript.json'
    story_cache      = CACHE_DIR / f'{episode_name}_story.json'
    meta_cache       = CACHE_DIR / f'{episode_name}_yt_meta.json'

    # --- Gemini key (only needed if story cache is missing) ---
    gemini_api_key = os.environ.get('GEMINI_API_KEY', '').strip()
    if not story_cache.exists() and not gemini_api_key:
        gemini_api_key = input('Paste your GEMINI_API_KEY: ').strip()
    if not story_cache.exists() and not gemini_api_key:
        print('GEMINI_API_KEY is required (no story cache found).')
        sys.exit(1)

    print(f'\nVideo   : {video_path.name}')
    print(f'Episode : {episode_name}')
    print(f'Whisper : {whisper_model}')
    print(f'Reframe : face_tracking (cv2, msec-seek patch)')

    import json

    work_dir = Path(tempfile.mkdtemp(prefix='shortgen_local_'))
    try:
        work_video = work_dir / f'{episode_name}.mp4'
        shutil.copy2(video_path, work_video)

        # --- Transcribe (cached) ---
        if transcript_cache.exists():
            print(f'\n[1/3] Transcription loaded from cache')
            data = json.loads(transcript_cache.read_text())
            segments, all_words = data['segments'], data['all_words']
            print(f'      {len(segments)} segments, {len(all_words)} words')
        else:
            print(f'\n[1/3] Transcribing with whisper-{whisper_model}...')
            segments, all_words = transcribe_video(str(work_video), model_size=whisper_model)
            print(f'      {len(segments)} segments, {len(all_words)} words')
            transcript_cache.write_text(json.dumps({'segments': segments, 'all_words': all_words}))
            print(f'      Saved to cache')

        # --- Analyze (cached) ---
        if story_cache.exists():
            print(f'\n[2/3] Gemini story loaded from cache')
            story = json.loads(story_cache.read_text())
        else:
            print('\n[2/3] Analyzing for best story arc (Gemini)...')
            story = analyze_transcript(segments, gemini_api_key)
            story_cache.write_text(json.dumps(story))
            print(f'      Saved to cache')

        total_sec = story.get('_total_seconds', 0)
        print(f'      [{story["score"]}/10] {story["storyline_title"]}  (~{total_sec:.0f}s)')
        print(f'      {story["storyline_summary"]}')
        print(f'      Series: {story.get("series_tag", "—")}')

        # --- YT metadata (cached) ---
        yt_meta = None
        if meta_cache.exists():
            print(f'      YT metadata loaded from cache')
            yt_meta = json.loads(meta_cache.read_text())
        elif gemini_api_key:
            print(f'      Generating YouTube metadata...')
            yt_meta = generate_yt_metadata(story, episode_name, gemini_api_key)
            if yt_meta:
                meta_cache.write_text(json.dumps(yt_meta))
        if yt_meta:
            print(f'      YT title   : {yt_meta.get("title", "")}')
            print(f'      Hook text  : {yt_meta.get("hook_text", "")}')

        print(f'      {len(story["clips"])} clips:')
        bad_clips = []
        for c in story['clips']:
            dur = c['end_seconds'] - c['start_seconds']
            flag = ' ⚠ DEGENERATE' if dur < 1.0 else ''
            print(f'        [{c["label"]}] {c["start_seconds"]:.1f}s-{c["end_seconds"]:.1f}s ({dur:.1f}s) — {c["description"]}{flag}')
            if dur < 1.0:
                bad_clips.append(c)
        if bad_clips:
            print(f'\n  *** WARNING: {len(bad_clips)}/{len(story["clips"])} clips are <1s — Gemini produced bad timestamps.')
            print(f'      Delete the story cache and re-run to get a fresh Gemini analysis:')
            print(f'      del "{story_cache}"')
            if len(bad_clips) == len(story['clips']):
                print(f'      Aborting — no usable clips.')
                sys.exit(1)

        # Extend each clip end by a tail pad so the natural sentence tone finishes.
        # 0.5 s is enough for a trailing syllable + breath without overlapping context.
        TAIL_PAD = 0.5
        _src_dur_r = _sp.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(work_video)],
            capture_output=True, text=True,
        )
        src_duration = float(_src_dur_r.stdout.strip()) if _src_dur_r.stdout.strip() else float('inf')
        for c in story['clips']:
            c['end_seconds'] = min(c['end_seconds'] + TAIL_PAD, src_duration - 0.05)

        # --- Create short (identical to GitHub: clipper → reframer → captioner) ---
        shorts_dir = work_dir / 'shorts'
        shorts_dir.mkdir()
        print(f'\n[3/3] Creating short...')
        short_path = create_story_short(
            str(work_video), story, str(shorts_dir),
            episode_name, pipeline=None, all_words=all_words,
        )

        out_file = OUTPUT_DIR / Path(short_path).name
        shutil.copy2(short_path, out_file)
        print(f'\nDone! -> {out_file}')

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
