import json
import re
import time
import requests

_GEMINI_URL = 'https://generativelanguage.googleapis.com/v1/models/gemini-2.0-flash:generateContent'

_PROMPT = """You are a video editor who creates viral short-form content from the TV show Friends (US sitcom, 1994-2004).

Analyze this timestamped transcript and identify the best segments for YouTube Shorts / Instagram Reels (30-90 seconds each).

Friends viral moment patterns — prioritize in this order:
1. Chandler's sarcastic one-liners ("Could this BE any more...")
2. Joey's food obsession, cluelessness, or lovably dumb moments
3. Ross's overreactions, "WE WERE ON A BREAK!", dinosaur nerd tangents
4. Monica's hyper-competitive or controlling moments
5. Phoebe's bizarre songs, conspiracy beliefs, or non-sequiturs ("Smelly Cat", "Unagi")
6. Rachel's fashion or relationship drama moments
7. Physical comedy with a clear visual payoff
8. Iconic recurring bits (Pivot!, The Routine, Unagi, etc.)
9. Group banter with a tight setup + punchline

Avoid:
- Pure scene-setting narration with no comedy or tension
- Scenes requiring heavy context to make sense
- Slow back-and-forth without a clear payoff
- Segments overlapping these already-used timestamps: {used_segments}

Transcript (format: [start_sec - end_sec] text):
{transcript}

Return ONLY a valid JSON array — no markdown, no explanation:
[
  {{
    "start_seconds": 45.2,
    "end_seconds": 87.0,
    "title": "joey_sandwich_crisis",
    "score": 9,
    "reason": "Joey food moment, works completely standalone"
  }}
]

Rules:
- 3 to 6 clips total
- Each clip: 20-90 seconds long
- Clips must NOT overlap
- Score 1-10 for viral potential (10 = highest)
- title: snake_case, max 30 chars"""


def analyze_transcript(segments, api_key, used_segments=None, retries=3):
    api_key = api_key.strip()

    transcript_text = '\n'.join(
        f"[{s['start']} - {s['end']}] {s['text']}"
        for s in segments
    )
    used_str = (
        ', '.join(f"{s:.0f}s-{e:.0f}s" for s, e in used_segments)
        if used_segments else 'none'
    )
    prompt = _PROMPT.format(transcript=transcript_text, used_segments=used_str)

    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.2},
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                _GEMINI_URL,
                params={'key': api_key},
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            raw = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
            raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
            clips = json.loads(raw)
            return sorted(clips, key=lambda x: x['score'], reverse=True)
        except requests.HTTPError as e:
            print(f"  Gemini attempt {attempt} failed: {e} — {e.response.text[:300]}")
            if attempt < retries:
                time.sleep(10)
        except Exception as e:
            print(f"  Gemini attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(10)

    raise RuntimeError("Gemini analysis failed after all retries")
