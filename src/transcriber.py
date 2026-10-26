import inspect
import os
from faster_whisper import WhisperModel

# int8 medium / medium.en needs ~2 GB of RAM while transcribing; below this a larger model is
# skipped in favour of the fallback rather than risking an out-of-memory kill mid-run.
_MIN_FREE_GB = {'medium': 2.5, 'medium.en': 2.5, 'large': 4.5}


def _free_gb():
    """Available RAM in GB, or None if it can't be read (then the model is just tried)."""
    try:
        if os.name == 'nt':
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return ms.ullAvailPhys / 1e9
            return None
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 1e6
    except Exception:
        return None
    return None


def runtime_allows(model_size):
    """False only when free RAM is known and below what the model needs."""
    need = next((gb for key, gb in sorted(_MIN_FREE_GB.items(), key=lambda kv: -len(kv[0]))
                 if model_size.startswith(key)), None)
    free = _free_gb()
    return need is None or free is None or free >= need


def _transcribe_kwargs(model, initial_prompt, hotwords):
    kw = {'beam_size': 5, 'word_timestamps': True}
    params = inspect.signature(model.transcribe).parameters
    if initial_prompt and 'initial_prompt' in params:
        kw['initial_prompt'] = initial_prompt
    if hotwords and 'hotwords' in params:   # faster-whisper >= 1.0.2
        kw['hotwords'] = hotwords
    return kw


def _run(video_path, model_size, initial_prompt, hotwords):
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    raw_segments, _ = model.transcribe(video_path, **_transcribe_kwargs(model, initial_prompt, hotwords))

    segments = []
    words = []
    for seg in raw_segments:
        segments.append({
            'start': round(seg.start, 2),
            'end':   round(seg.end, 2),
            'text':  seg.text.strip(),
        })
        if seg.words:
            for w in seg.words:
                words.append({
                    'word':  w.word.strip(),
                    'start': round(w.start, 3),
                    'end':   round(w.end, 3),
                })
    return segments, words


def transcribe_video(video_path, model_size="base", initial_prompt=None, hotwords=None,
                     fallback_models=(), info=None):
    """
    Returns (segments, words) where segments=[{start,end,text}] and words=[{word,start,end}].

    initial_prompt / hotwords prime Whisper with the campaign's names and terms
    (campaigns.whisper_hints). model_size is tried first, then each of fallback_models
    (a model the runtime can't hold, or that fails to download or load, falls through).
    info, if given, gets info['model'] = the model that produced the transcript.
    """
    models = [model_size] + [m for m in (fallback_models or ()) if m and m != model_size]
    last_err = None
    for i, m in enumerate(models):
        is_last = i == len(models) - 1
        if not is_last and not runtime_allows(m):
            print(f"      whisper-{m}: not enough free RAM ({_free_gb():.1f} GB) — using {models[i + 1]}")
            continue
        try:
            out = _run(video_path, m, initial_prompt, hotwords)
        except Exception as e:
            if is_last:
                raise
            last_err = e
            print(f"      whisper-{m} failed ({e}) — falling back to {models[i + 1]}")
            continue
        if info is not None:
            info['model'] = m
        return out
    raise RuntimeError(f'no Whisper model could transcribe {video_path}: {last_err}')
