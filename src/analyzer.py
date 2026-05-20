import json
import re
import time
import google.generativeai as genai

_PROMPT = """You are a video editor who creates viral short-form content from TV show episodes.

Analyze this timestamped transcript and identify the best segments for YouTube Shorts / Instagram Reels.

Pick segments that are:
- Standalone funny exchanges (land without heavy context)
- Emotional reveals or confessions
- Iconic one-liners or memorable moments
- Clear setup + punchline structure

Avoid:
- Pure narrated exposition with no action
- Scenes that only make sense mid-episode with no payoff
- Repetitive or slow scenes

Transcript (format: [start_sec - end_sec] text):
{transcript}

Return ONLY a valid JSON array with no markdown fences or explanation:
[
  {{
    "start_seconds": 45.2,
    "end_seconds": 87.0,
    "title": "barney_suit_up",
    "score": 9,
    "reason": "Iconic suit up bit, works standalone"
  }}
]

Rules:
- 3 to 6 clips total
- Each clip: 20-90 seconds long
- Clips must NOT overlap
- Score 1-10 for viral potential (10 = highest)
- title: snake_case, max 30 chars, no spaces"""


def analyze_transcript(segments, api_key, retries=3):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')

    transcript_text = '\n'.join(
        f"[{s['start']} - {s['end']}] {s['text']}"
        for s in segments
    )

    prompt = _PROMPT.format(transcript=transcript_text)

    for attempt in range(1, retries + 1):
        try:
            response = model.generate_content(prompt)
            raw = response.text.strip()
            # Strip accidental markdown code fences
            raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
            raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
            clips = json.loads(raw)
            # Sort highest score first
            return sorted(clips, key=lambda x: x['score'], reverse=True)
        except Exception as e:
            print(f"  Gemini attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(5)

    raise RuntimeError("Gemini analysis failed after all retries")
