import os
import subprocess


def create_shorts(video_path, clips, output_dir, episode_name):
    """Cut clips and convert each to vertical 9:16 with blurred background."""
    paths = []
    for i, clip in enumerate(clips, 1):
        title = clip['title']
        start = clip['start_seconds']
        end = clip['end_seconds']
        score = clip['score']

        raw = os.path.join(output_dir, f"_raw_{i}.mp4")
        out = os.path.join(output_dir, f"{episode_name}_{i:02d}_{title}_s{score}.mp4")

        print(f"  Cutting clip {i}: {title} ({start:.1f}s – {end:.1f}s)")
        _cut(video_path, raw, start, end)

        print(f"  Making vertical: {os.path.basename(out)}")
        _make_vertical(raw, out)

        os.remove(raw)
        paths.append(out)

    return paths


def _cut(src, dst, start, end):
    _run([
        'ffmpeg', '-y',
        '-ss', str(start),
        '-to', str(end),
        '-i', src,
        '-c', 'copy',
        dst,
    ])


def _make_vertical(src, dst):
    # Scale original to fill 1080x1920, blur it as background.
    # Overlay the properly-fitted original video centered on top.
    filter_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=30:30[bg];"
        "[0:v]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[out]"
    )
    _run([
        'ffmpeg', '-y',
        '-i', src,
        '-filter_complex', filter_complex,
        '-map', '[out]',
        '-map', '0:a?',
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-crf', '23',
        '-c:a', 'aac',
        dst,
    ])


def _run(cmd):
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed:\n{result.stderr.decode(errors='replace')}"
        )
