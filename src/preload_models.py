"""Pre-downloads Whisper + pyannote + mediapipe models before main.py runs."""
import os

whisper_model = os.environ.get('WHISPER_MODEL', 'small')
hf_token      = os.environ.get('HF_TOKEN', '').strip()

print('Pre-downloading face_landmarker model...')
from reframer import _ensure_landmarker_model
_ensure_landmarker_model()
print('  Face landmarker done.')

print(f'Pre-downloading whisper-{whisper_model}...')
from faster_whisper import WhisperModel
WhisperModel(whisper_model, device='cpu', compute_type='int8')
print('  Whisper done.')

# Talk / podcast campaigns prefer an English-only model (campaigns.whisper_models); a failed
# download here is not fatal: the run falls back to WHISPER_MODEL.
talk_model = os.environ.get('WHISPER_TALK_MODEL', '').strip()
if talk_model and talk_model.lower() != 'off' and talk_model != whisper_model:
    print(f'Pre-downloading whisper-{talk_model} (talk campaigns)...')
    try:
        WhisperModel(talk_model, device='cpu', compute_type='int8')
        print('  Talk model done.')
    except Exception as e:
        print(f'  Talk model download failed: {e} (talk campaigns fall back to whisper-{whisper_model})')

if hf_token:
    print('Pre-downloading pyannote/speaker-diarization-3.1...')
    try:
        from pyannote.audio import Pipeline
        Pipeline.from_pretrained('pyannote/speaker-diarization-3.1')
        print('  Pyannote done.')
    except Exception as e:
        print(f'  Pyannote download failed: {e}')
        print('  Run will use face-only tracking. Accept all gated models on HuggingFace.')
else:
    print('HF_TOKEN not set — skipping pyannote download (face-only tracking will be used).')
