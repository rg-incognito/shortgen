import os
import subprocess
import tempfile


def create_story_short(video_path, story, output_dir, episode_name, pipeline=None):
    """
    Cuts every clip in a story arc, reframes each to 9:16, then concatenates
    into one compiled video that tells the complete storyline.
    """
    from reframer import reframe_clip

    clips = story['clips']
    title = story['storyline_title']
    score = story['score']

    parts_dir = os.path.join(output_dir, '_parts')
    os.makedirs(parts_dir, exist_ok=True)

    part_paths = []
    try:
        for i, clip in enumerate(clips, 1):
            part_path = os.path.join(parts_dir, f"part_{i:02d}.mp4")
            print(f"  Part {i}/{len(clips)}: [{clip['label']}] "
                  f"{clip['start_seconds']:.1f}s – {clip['end_seconds']:.1f}s")
            reframe_clip(video_path, clip['start_seconds'], clip['end_seconds'],
                         part_path, pipeline=pipeline)
            part_paths.append(part_path)

        out_path = os.path.join(output_dir, f"{episode_name}_{title}_s{score}.mp4")
        print(f"  Merging {len(part_paths)} parts → {os.path.basename(out_path)}")
        _merge_clips(part_paths, out_path)
        return out_path

    finally:
        for p in part_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(parts_dir)
        except OSError:
            pass


def _merge_clips(clip_paths, output_path):
    """Concatenate same-format clips using ffmpeg concat demuxer (no re-encode)."""
    list_file = tempfile.mktemp(suffix='.txt')
    try:
        with open(list_file, 'w') as f:
            for p in clip_paths:
                # escape single quotes in path for ffmpeg concat list
                safe = p.replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
        subprocess.run([
            'ffmpeg', '-y',
            '-f', 'concat', '-safe', '0',
            '-i', list_file,
            '-c', 'copy',
            output_path,
        ], check=True, capture_output=True)
    finally:
        try:
            os.unlink(list_file)
        except OSError:
            pass
