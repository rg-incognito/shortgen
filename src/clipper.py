import os
import subprocess


def create_shorts(video_path, clips, output_dir, episode_name, pipeline=None):
    """Cut clips and reframe each to vertical 9:16 with speaker-focused crop."""
    from reframer import reframe_clip

    paths = []
    for i, clip in enumerate(clips, 1):
        title = clip['title']
        start = clip['start_seconds']
        end   = clip['end_seconds']
        score = clip['score']
        out   = os.path.join(output_dir, f"{episode_name}_{i:02d}_{title}_s{score}.mp4")

        print(f"  Cutting clip {i}: {title} ({start:.1f}s – {end:.1f}s)")
        print(f"  Making vertical: {os.path.basename(out)}")
        reframe_clip(video_path, start, end, out, pipeline=pipeline)
        paths.append(out)

    return paths
