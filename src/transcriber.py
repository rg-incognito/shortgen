import os
from faster_whisper import WhisperModel


def transcribe_video(video_path, model_size="base"):
    """Returns list of {start, end, text} dicts."""
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(video_path, beam_size=5)

    result = []
    for seg in segments:
        result.append({
            'start': round(seg.start, 2),
            'end': round(seg.end, 2),
            'text': seg.text.strip(),
        })

    return result
