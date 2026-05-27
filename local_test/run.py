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

from transcriber import transcribe_video
from analyzer import analyze_transcript
from clipper import create_story_short

INPUT_DIR  = Path(__file__).parent / 'input'
OUTPUT_DIR = Path(__file__).parent / 'output'
OUTPUT_DIR.mkdir(exist_ok=True)


def main():
    # --- Gemini key ---
    gemini_api_key = os.environ.get('GEMINI_API_KEY', '').strip()
    if not gemini_api_key:
        gemini_api_key = input('Paste your GEMINI_API_KEY: ').strip()
    if not gemini_api_key:
        print('GEMINI_API_KEY is required.')
        sys.exit(1)

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

    episode_name = video_path.stem.replace(' ', '_')
    print(f'\nVideo   : {video_path.name}')
    print(f'Episode : {episode_name}')
    print(f'Whisper : {whisper_model}')

    work_dir = Path(tempfile.mkdtemp(prefix='shortgen_local_'))
    try:
        work_video = work_dir / f'{episode_name}.mp4'
        shutil.copy2(video_path, work_video)

        # --- Transcribe ---
        print(f'\n[1/3] Transcribing with whisper-{whisper_model}...')
        segments, all_words = transcribe_video(str(work_video), model_size=whisper_model)
        print(f'      {len(segments)} segments, {len(all_words)} words')

        # --- Analyze ---
        print('\n[2/3] Analyzing for best story arc (Gemini)...')
        story = analyze_transcript(segments, gemini_api_key)
        total_sec = story.get('_total_seconds', 0)
        print(f'      [{story["score"]}/10] {story["storyline_title"]}  (~{total_sec:.0f}s)')
        print(f'      {story["storyline_summary"]}')
        print(f'      Series: {story.get("series_tag", "—")}')
        print(f'      {len(story["clips"])} clips:')
        for c in story['clips']:
            dur = c['end_seconds'] - c['start_seconds']
            print(f'        [{c["label"]}] {c["start_seconds"]:.0f}s-{c["end_seconds"]:.0f}s ({dur:.0f}s) — {c["description"]}')

        # --- Create short ---
        shorts_dir = work_dir / 'shorts'
        shorts_dir.mkdir()
        print('\n[3/3] Creating short...')
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
