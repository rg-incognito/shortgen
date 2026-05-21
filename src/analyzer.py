import json
import re
import time
import requests

_GEMINI_URL = 'https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent'

_PROMPT = """You are a viral short-form video editor working with Friends (US sitcom, 1994-2004).

Your task: Find the SINGLE BEST storyline in this episode — one continuous subplot, running joke, or character arc — and identify ALL scene segments from across the whole episode that build and pay it off.

The output will be ONE compiled video. ALL selected clips must belong to the SAME storyline and together tell a complete story: setup → escalation → payoff (and aftermath if present).

Great storylines to look for (in priority order):
1. A character does something embarrassing/dumb that escalates across scenes (e.g. Ross can't cancel gym membership, Joey eats someone's sandwich, Chandler lies and digs deeper)
2. Phoebe's bizarre behaviour that culminates in an absurd payoff (biting a client, Smelly Cat negotiation, Unagi)
3. A scheme or deception that gradually unravels with group reactions
4. A physical comedy bit that builds across multiple scenes
5. A misunderstanding that compounds throughout the episode
6. One character trying (and failing) to teach or change another

Transcript (format: [start_sec - end_sec] text):
{transcript}

Previously used segments (AVOID all of these — do not include any clip that overlaps with them): {used_segments}

Return ONLY a valid JSON object — no markdown, no explanation:
{{
  "storyline_title": "phoebe_bites_massage_client",
  "storyline_summary": "Phoebe bites her massage client, panics, tries to hide it, then confesses — plus the group's reactions",
  "clips": [
    {{
      "start_seconds": 45.2,
      "end_seconds": 87.0,
      "label": "setup",
      "description": "Phoebe introduces her unorthodox massage philosophy to the gang"
    }}
  ],
  "score": 9
}}

Rules:
- 3 to 8 clips in CHRONOLOGICAL ORDER
- Each clip: 10-90 seconds long
- Clips must NOT overlap each other or the previously-used segments
- First clip must establish the premise/setup
- Last clip must deliver a clear payoff or resolution
- Score 1-10 for viral potential as a compiled story (10 = highest)
- storyline_title: snake_case, max 40 chars"""


def analyze_transcript(segments, api_key, used_segments=None, retries=5):
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
            story = json.loads(raw)
            return story
        except requests.HTTPError as e:
            print(f"  Gemini attempt {attempt} failed: {e} — {e.response.text[:300]}")
            if attempt < retries:
                wait = 60 if e.response.status_code in (503, 429) else 15
                time.sleep(wait)
        except Exception as e:
            print(f"  Gemini attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(15)

    raise RuntimeError("Gemini analysis failed after all retries")
