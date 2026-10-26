# SFX used by the edit layer (src/clipper.py `_SFX`)

Copied unchanged from `shortGen_work/sfx_kit/` (2026-10-26). All files are 48 kHz / 24-bit
stereo WAV, normalised to -16 LUFS integrated, true peak <= -1 dBTP. The clipper trims each to
a short window around its peak and mixes it under the original audio (never replacing it).

All five are **procedurally generated, our own work** (numpy/scipy synthesis by
`sfx_kit/_generator/gen_sfx.py`, seed 20261026): no samples, recordings or meme audio were
used. Free for commercial use, no attribution, no claim risk. Treat as CC0.

| File | Used for | Generator | License |
|---|---|---|---|
| whoosh_1_short.wav | whoosh on a cut (`whoosh`) | `whoosh` | own work; treat as CC0 |
| whoosh_2_medium.wav | whoosh on a cut, alternate (`whoosh2`) | `whoosh` | own work; treat as CC0 |
| swoosh_transition_1_up.wav | the flash-transition cut (`flash`) | `swoosh_transition`, rising | own work; treat as CC0 |
| pop_1_bubble.wav | the headline pop at frame 0 (`pop`) | `pop` | own work; treat as CC0 |
| boom_1_bass_hit.wav | the payoff hit (`boom`) | `sub_boom` | own work; treat as CC0 |

The rest of the kit (dings, risers, record scratch, Kenney CC0 clicks...) is not used yet; see
`shortGen_work/sfx_kit/LICENSES.md` for its sources. The tracks in `music/` are not used anywhere:
they have no provenance, and the music bed only plays tracks listed in `music/LICENSES.json`.
