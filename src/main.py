import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from drive_client import download_video, upload_shorts, pick_video, move_to_archive
from transcriber import transcribe_video
from analyzer import analyze_transcript
from clipper import create_shorts
from sheet_tracker import SheetTracker

VIDEOS_FOLDER_ID  = os.environ.get('VIDEOS_FOLDER_ID',  '1bIFaNpfBy1Bp9R14T22EV-hMsYYrLeTS')
SHORTS_FOLDER_ID  = os.environ.get('SHORTS_FOLDER_ID',  '1kzFXWZQu8QQVtZKeAVvSgHcLaRFfK6tg')
ARCHIVE_FOLDER_ID = os.environ.get('ARCHIVE_FOLDER_ID', '1nHZCaWO_KvkV52TekkF8ecC5JtIlXqJj')
MAX_RUNS = 2


def main():
    service_account_info = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'].lstrip('﻿'))
    gemini_api_key        = os.environ['GEMINI_API_KEY']
    spreadsheet_id        = os.environ['SPREADSHEET_ID'].strip()
    whisper_model         = os.environ.get('WHISPER_MODEL', 'small')

    video_file_id = os.environ.get('VIDEO_FILE_ID', '').strip()
    episode_name  = os.environ.get('EPISODE_NAME', '').strip()

    tracker = SheetTracker(service_account_info, spreadsheet_id)

    # Auto-pick if no file ID provided
    if not video_file_id:
        print("[0/5] Auto-picking video from videos/ folder...")
        video_file_id, episode_name = pick_video(VIDEOS_FOLDER_ID, service_account_info, tracker)
        if not video_file_id:
            print("No unprocessed videos in videos/ folder. Upload one and re-run.")
            sys.exit(0)
        print(f"      Picked: {episode_name}")
    elif not episode_name:
        episode_name = 'episode'

    episode_name = episode_name.replace(' ', '_')

    prior_runs    = tracker.get_run_count(episode_name)
    current_run   = prior_runs + 1
    used_segments = tracker.get_used_segments(episode_name)

    print(f"      Episode : {episode_name}")
    print(f"      Run     : {current_run} of {MAX_RUNS}")
    print(f"      Avoiding: {len(used_segments)} previously used segment(s)")

    work_dir = Path(tempfile.mkdtemp(prefix='shortgen_'))
    try:
        video_path = work_dir / f"{episode_name}.mp4"

        print(f"[1/5] Downloading video...")
        download_video(video_file_id, str(video_path), service_account_info)
        print(f"      {video_path.stat().st_size / 1e6:.1f} MB")

        print(f"[2/5] Transcribing with whisper-{whisper_model}...")
        segments = transcribe_video(str(video_path), model_size=whisper_model)
        print(f"      {len(segments)} segments transcribed")

        print("[3/5] Analyzing for viral moments (Gemini)...")
        clips = analyze_transcript(segments, gemini_api_key, used_segments=used_segments)
        print(f"      {len(clips)} clips selected:")
        for c in clips:
            print(f"        [{c['score']}/10] {c['title']}  {c['start_seconds']:.0f}s-{c['end_seconds']:.0f}s — {c['reason']}")

        shorts_dir = work_dir / 'shorts'
        shorts_dir.mkdir()
        print("[4/5] Creating shorts...")
        short_paths = create_shorts(str(video_path), clips, str(shorts_dir), episode_name)

        print(f"[5/5] Uploading {len(short_paths)} shorts to Drive...")
        upload_shorts(short_paths, SHORTS_FOLDER_ID, service_account_info)

        tracker.log_clips(episode_name, current_run, clips)
        print(f"      Logged run {current_run} to tracker sheet")

        if current_run >= MAX_RUNS:
            print(f"      Run {current_run} = max runs reached — archiving video...")
            move_to_archive(video_file_id, ARCHIVE_FOLDER_ID, VIDEOS_FOLDER_ID, service_account_info)
            print(f"      Moved to archive/")

        print(f"\nDone! {len(short_paths)} shorts in Drive → series_shorts/shorts/")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
