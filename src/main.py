import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from drive_client import download_video, upload_shorts
from transcriber import transcribe_video
from analyzer import analyze_transcript
from clipper import create_shorts


def main():
    video_file_id = os.environ['VIDEO_FILE_ID']
    output_folder_id = os.environ['OUTPUT_FOLDER_ID']
    episode_name = os.environ.get('EPISODE_NAME', 'episode').replace(' ', '_')
    whisper_model = os.environ.get('WHISPER_MODEL', 'base')
    service_account_info = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
    gemini_api_key = os.environ['GEMINI_API_KEY']

    work_dir = Path(tempfile.mkdtemp(prefix='shortgen_'))

    try:
        video_path = work_dir / f"{episode_name}.mp4"
        print(f"[1/5] Downloading video (file id: {video_file_id})...")
        download_video(video_file_id, str(video_path), service_account_info)
        print(f"      Saved to {video_path} ({video_path.stat().st_size / 1e6:.1f} MB)")

        print(f"[2/5] Transcribing with whisper-{whisper_model}...")
        segments = transcribe_video(str(video_path), model_size=whisper_model)
        print(f"      {len(segments)} segments transcribed")

        print("[3/5] Analyzing transcript for viral moments (Gemini)...")
        clips = analyze_transcript(segments, gemini_api_key)
        print(f"      Selected {len(clips)} clips:")
        for c in clips:
            print(f"        [{c['score']}/10] {c['title']} — {c['reason']}")

        shorts_dir = work_dir / 'shorts'
        shorts_dir.mkdir()
        print("[4/5] Creating shorts...")
        short_paths = create_shorts(
            str(video_path), clips, str(shorts_dir), episode_name
        )

        print(f"[5/5] Uploading {len(short_paths)} shorts to Drive folder {output_folder_id}...")
        upload_shorts(short_paths, output_folder_id, service_account_info)

        print("\nDone! Shorts are in your Drive folder.")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
