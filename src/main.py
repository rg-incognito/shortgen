import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from drive_client import (download_video, upload_shorts, pick_video, pick_from_files,
                          move_to_archive, move_to_used_folder,
                          load_transcript_cache, save_transcript_cache)
from transcriber import transcribe_video
from analyzer import analyze_transcript, generate_yt_metadata
from clipper import create_story_short, edited_duration
from sheet_tracker import SheetTracker
from campaigns import (get_campaign, find_banned, find_transcript_banned, enforce_caption,
                       next_publish_time)
from footage import download_from_release, sponsor_segments

VIDEOS_FOLDER_ID  = os.environ.get('VIDEOS_FOLDER_ID',  '1bIFaNpfBy1Bp9R14T22EV-hMsYYrLeTS')
SHORTS_FOLDER_ID  = os.environ.get('SHORTS_FOLDER_ID',  '1kzFXWZQu8QQVtZKeAVvSgHcLaRFfK6tg')
ARCHIVE_FOLDER_ID = os.environ.get('ARCHIVE_FOLDER_ID', '1nHZCaWO_KvkV52TekkF8ecC5JtIlXqJj')
TAIL_PAD = 0.5


def _probe_duration(path):
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
        capture_output=True, text=True,
    )
    return float(out.stdout.strip()) if out.stdout.strip() else float('inf')


def _pick_story(segments, all_words, gemini_api_key, used_segments, used_storylines,
                campaign, profile, src_dur, attempts=4):
    """
    Ask Gemini for a moment, then check it against the campaign's hard limits
    (edited length, banned words in the hook). A rejected moment is added to
    the avoid-list so the next attempt picks something else.
    """
    lo, hi = profile.get('min_seconds'), profile.get('max_seconds')
    avoid = list(used_segments)
    for attempt in range(1, attempts + 1):
        story = analyze_transcript(segments, gemini_api_key, used_segments=avoid,
                                   used_storylines=used_storylines, campaign=campaign)
        # Extend each clip end by a tail pad so sentence tone finishes naturally.
        for c in story['clips']:
            c['end_seconds'] = min(c['end_seconds'] + TAIL_PAD, src_dur - 0.05)

        problems = []
        if lo or hi:
            dur = edited_duration(story, all_words, profile['silence_thresh'])
            story['_edited_seconds'] = round(dur, 1)
            if lo and dur < lo:
                problems.append(f"too short after trimming ({dur:.1f}s < {lo}s)")
            if hi and dur > hi:
                problems.append(f"too long after trimming ({dur:.1f}s > {hi}s)")
        banned = find_banned(story.get('hook_text', ''), profile)
        if banned:
            problems.append(f"hook uses banned words {banned}")
        ad_hits = find_transcript_banned(story, all_words, profile)
        if ad_hits:
            problems.append(f"clip audio looks like an ad read {ad_hits}")

        if not problems:
            return story
        print(f"      Attempt {attempt} rejected: {'; '.join(problems)}")
        avoid += [(c['start_seconds'], c['end_seconds']) for c in story['clips']]

    raise RuntimeError(f"No moment met the {campaign} campaign rules after {attempts} attempts")


def _make_metadata(story, episode_name, gemini_api_key, campaign, profile, attempts=3):
    """YouTube metadata that satisfies the campaign's caption rules, or None."""
    for attempt in range(1, attempts + 1):
        yt_meta = generate_yt_metadata(story, episode_name, gemini_api_key, campaign=campaign)
        if not yt_meta:
            return None
        text = ' '.join(str(yt_meta.get(k, '')) for k in ('title', 'description', 'tags', 'pinned_comment'))
        banned = find_banned(text, profile)
        if not banned:
            return enforce_caption(yt_meta, profile)
        print(f"      Metadata attempt {attempt} rejected: banned words {banned}")
    raise RuntimeError(f"Could not generate {campaign} metadata without banned words")


def main():
    campaign, profile = get_campaign(os.environ.get('CAMPAIGN'))
    max_runs = profile['max_runs']
    print(f"Campaign: {campaign}")

    service_account_info = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'].lstrip('﻿'))
    gemini_api_key        = os.environ['GEMINI_API_KEY']
    spreadsheet_id        = os.environ['SPREADSHEET_ID'].strip()
    whisper_model         = os.environ.get('WHISPER_MODEL', 'small')
    hf_token              = os.environ.get('HF_TOKEN', '').strip()

    video_file_id = os.environ.get('VIDEO_FILE_ID', '').strip()
    episode_name  = os.environ.get('EPISODE_NAME', '').strip()

    tracker = SheetTracker(service_account_info, spreadsheet_id)

    diarization_pipeline = None
    if hf_token:
        try:
            from reframer import load_diarization_pipeline
            diarization_pipeline = load_diarization_pipeline(hf_token)
            print('  Diarization pipeline loaded (speaker-aware tracking active)')
        except Exception as e:
            print(f'  Diarization pipeline unavailable: {e} — using face-only tracking')

    # GitHub cron runs can start hours late and the local fallback may also fire;
    # never put two Shorts of one campaign into the same publish slot.
    slot = next_publish_time(profile)
    if slot and not video_file_id and tracker.slot_taken({ep for _, ep in profile.get('files', [])}, slot):
        print(f"{campaign}: a Short is already scheduled for the {slot} slot — nothing to do.")
        sys.exit(0)

    # Auto-pick if no file ID provided
    if not video_file_id:
        if profile['source'] in ('drive_files', 'release'):
            print(f"[0/5] Auto-picking {campaign} episode...")
            video_file_id, episode_name = pick_from_files(profile['files'], tracker, max_runs)
            if not video_file_id:
                print(f"Every {campaign} episode has reached {max_runs} runs — add footage or raise max_runs.")
                sys.exit(0)
        else:
            print("[0/5] Auto-picking video from videos/ folder...")
            video_file_id, episode_name = pick_video(VIDEOS_FOLDER_ID, service_account_info, tracker, max_runs=max_runs)
            if not video_file_id:
                print("No unprocessed videos in videos/ folder. Upload one and re-run.")
                sys.exit(0)
        print(f"      Picked: {episode_name}")
    elif not episode_name:
        episode_name = 'episode'

    episode_name = episode_name.replace(' ', '_')

    prior_runs       = tracker.get_run_count(episode_name)
    current_run      = prior_runs + 1
    used_segments    = tracker.get_used_segments(episode_name)
    used_storylines  = tracker.get_used_storylines(episode_name)

    print(f"      Episode : {episode_name}")
    print(f"      Run     : {current_run} of {max_runs}")
    print(f"      Avoiding: {len(used_segments)} previously used segment(s)")

    work_dir = Path(tempfile.mkdtemp(prefix='shortgen_'))
    try:
        video_path = work_dir / f"{episode_name}.mp4"

        print("[1/5] Downloading video...")
        if profile['source'] == 'release':
            download_from_release(profile['release_tag'], f"{video_file_id}.mp4", str(video_path))
        else:
            download_video(video_file_id, str(video_path), service_account_info)
        print(f"      {video_path.stat().st_size / 1e6:.1f} MB")

        # Long campaign episodes get clipped many times — transcribe each one once.
        cache_name = f"{episode_name}_{whisper_model}.json"
        cached = None
        try:
            cached = load_transcript_cache(cache_name, SHORTS_FOLDER_ID)
        except Exception as e:
            print(f"      Transcript cache unavailable: {e}")
        if cached:
            segments, all_words = cached['segments'], cached['words']
            print(f"[2/5] Using cached transcript ({len(segments)} segments, {len(all_words)} words)")
        else:
            print(f"[2/5] Transcribing with whisper-{whisper_model}...")
            segments, all_words = transcribe_video(str(video_path), model_size=whisper_model)
            print(f"      {len(segments)} segments, {len(all_words)} words transcribed")
            try:
                save_transcript_cache(cache_name, SHORTS_FOLDER_ID, segments, all_words)
            except Exception as e:
                print(f"      Transcript cache save skipped: {e}")

        print("[3/5] Analyzing for best story arc (Gemini)...")
        src_dur = _probe_duration(video_path)
        avoid_segments = list(used_segments)
        if profile.get('sponsorblock'):
            ads = sponsor_segments(video_file_id)
            print(f"      Avoiding {len(ads)} known ad-read segment(s) (SponsorBlock)")
            avoid_segments += ads
        story = _pick_story(segments, all_words, gemini_api_key, avoid_segments, used_storylines,
                            campaign, profile, src_dur)
        total_sec = story.get('_edited_seconds', story.get('_total_seconds', 0))
        print(f"      Story: [{story['score']}/10] ({story.get('format', 'story_recap')}) {story['storyline_title']}  (~{total_sec:.0f}s total)")
        print(f"      Summary: {story['storyline_summary']}")
        print(f"      {len(story['clips'])} clips:")
        for c in story['clips']:
            dur = c['end_seconds'] - c['start_seconds']
            print(f"        [{c['label']}] {c['start_seconds']:.0f}s-{c['end_seconds']:.0f}s ({dur:.0f}s) — {c['description']}")

        print("      Generating YouTube metadata...")
        yt_meta = _make_metadata(story, episode_name, gemini_api_key, campaign, profile)
        if yt_meta:
            print(f"      YT title: {yt_meta.get('title', '')}")

        shorts_dir = work_dir / 'shorts'
        shorts_dir.mkdir()
        print("[4/5] Creating story compilation...")
        short_path = create_story_short(str(video_path), story, str(shorts_dir),
                                        episode_name, pipeline=diarization_pipeline,
                                        all_words=all_words,
                                        silence_thresh=profile['silence_thresh'],
                                        wide_fallback=profile.get('wide_fallback', False))

        if profile.get('min_seconds') or profile.get('max_seconds'):
            final_dur = _probe_duration(short_path)
            print(f"      Final length: {final_dur:.1f}s")
            if ((profile.get('min_seconds') and final_dur < profile['min_seconds'] - 1) or
                    (profile.get('max_seconds') and final_dur > profile['max_seconds'] + 2)):
                raise RuntimeError(f"Rendered short is {final_dur:.1f}s — outside the {campaign} brief's limits")

        print("[5/5] Uploading to Drive...")
        uploaded = upload_shorts([short_path], SHORTS_FOLDER_ID, service_account_info)
        drive_file_id = uploaded[0][1] if uploaded else None

        _SERIES_PLAYLIST = {
            'ross_being_ross':         'Ross Being Ross',
            'joey_being_joey':         'Joey Being Joey',
            'chandler_being_chandler': 'Chandler Being Chandler',
            'phoebe_being_phoebe':     'Phoebe Being Phoebe',
            'monica_being_monica':     'Monica Being Monica',
            'rachel_being_rachel':     'Rachel Being Rachel',
            'relationship_drama':      'Relationship Drama',
            'gang_together':           'The Gang',
        }

        # Upload to YouTube as a scheduled post (requires YT_TOKEN_JSON secret)
        yt_video_id = None
        if yt_meta:
            # Row must exist before update_yt_url fills in scheduled_at / yt_url.
            tracker.log_posting_metadata(episode_name, current_run, story, yt_meta)
            from youtube_client import upload_scheduled, add_to_playlist
            publish_at = next_publish_time(profile) or tracker.get_next_publish_slot()
            print(f"[5b] Scheduling on YouTube → {publish_at}...")
            yt_video_id = upload_scheduled(
                short_path,
                yt_meta['title'],
                yt_meta['description'],
                yt_meta['tags'],
                publish_at,
            )
            print(f"      https://youtu.be/{yt_video_id}")
            tracker.update_yt_url(episode_name, current_run,
                                  story['storyline_title'], yt_video_id, publish_at)

            series_tag = story.get('series_tag', '')
            playlist_title = profile.get('playlist') or _SERIES_PLAYLIST.get(series_tag)
            if playlist_title:
                # YouTube often returns a transient 409 SERVICE_UNAVAILABLE right after upload.
                for attempt in (1, 2):
                    try:
                        add_to_playlist(yt_video_id, playlist_title)
                        print(f"      Added to playlist: {playlist_title}")
                        break
                    except Exception as pe:
                        if attempt == 2:
                            print(f"      Playlist add skipped: {pe}")
                        else:
                            time.sleep(20)

        # Move the uploaded short to shorts/used/
        if drive_file_id:
            try:
                move_to_used_folder(drive_file_id, SHORTS_FOLDER_ID)
                print("      Moved to shorts/used/")
            except Exception as e:
                print(f"      Move to used/ skipped: {e}")

        # Log each clip segment individually so future runs avoid them all
        clips_for_log = [
            {
                'title':         f"{story['storyline_title']}_p{i + 1}",
                'start_seconds': c['start_seconds'],
                'end_seconds':   c['end_seconds'],
                'score':         story['score'],
                'reason':        c['description'],
            }
            for i, c in enumerate(story['clips'])
        ]
        tracker.log_clips(episode_name, current_run, clips_for_log)
        print(f"      Logged run {current_run} to tracker sheet")

        if current_run >= max_runs and profile['archive']:
            print(f"      Run {current_run} = max runs reached — archiving video...")
            move_to_archive(video_file_id, ARCHIVE_FOLDER_ID, VIDEOS_FOLDER_ID, service_account_info)
            print(f"      Moved to archive/")

        print(f"\nDone! 1 story short in Drive → series_shorts/shorts/")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
