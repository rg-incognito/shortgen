"""
Cut the Jacob & Co x Kylie Jenner Fantasy Bra Shorts (Clip Farm campaign) from the brand's Dropbox footage.

    python jacobco_edits.py <kylie_videos_dir> <craft_videos_dir> <out_dir>

The footage has no speech, so the normal transcript pipeline can't cut it; each Short is a fixed
edit list. Every Short carries one of the brief's required on-screen headlines, word for word.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
FONT = 'C:/Windows/Fonts/impact.ttf'

# (file key, start s, duration s); 16:9 sources are centre-cropped to 9:16.
EDITS = {
    'JACOBCO_runway': {
        'headline': "KYLIE JENNER WALKS THE VICTORIA'S SECRET RUNWAY IN A $50 MILLION FANTASY BRA CREATED BY JACOB & CO.",
        'segments': [('opening', 0, 11.7), ('closeup', 0, 4.4), ('full', 60.5, 7)],  # 57-60 s is a crowd cutaway
    },
    'JACOBCO_reveal': {
        'headline': "KYLIE JENNER WEARS A $50 MILLION BRA DOWN THE VICTORIA'S SECRET RUNWAY, CREATED BY JACOB&CO",
        'segments': [('full', 30, 18.5), ('closeup', 0, 4.4)],
    },
    'JACOBCO_story': {
        'headline': "JACOB & CO. CREATES A $50 MILLION FANTASY BRA FOR THE 2026 VICTORIA'S SECRET FASHION SHOW, WORN BY KYLIE JENNER",
        'segments': [('craft', 0, 14), ('assembly', 0, 8), ('opening', 2, 9.7), ('closeup', 0, 4.4)],
    },
}

SOURCES = {
    'opening':  ('kylie', 'KylieJenner_Opening_VSFS26.mp4'),
    'closeup':  ('kylie', 'KylieJenner_WinterBra_VSFS26.mp4'),
    'full':     ('kylie', 'Kylie Jenner_Runway_Winter_16x9_Full Clip.mp4'),
    'craft':    ('craft', '9x16 260728_BS_CRAFT v6.mp4'),
    'assembly': ('craft', '9x16 Bra Assembly v3.mp4'),
}
WIDE = {'full'}


def headline_png(text, path):
    """Transparent full-frame overlay: the headline, wrapped, in the upper safe area."""
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 64)
    lines, line = [], ''
    for word in text.split():
        trial = f'{line} {word}'.strip()
        if draw.textlength(trial, font=font) > W - 140:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    y = 230
    for ln in lines:
        x = (W - draw.textlength(ln, font=font)) / 2
        draw.text((x, y), ln, font=font, fill='white', stroke_width=6, stroke_fill='black')
        y += 76
    img.save(path)


def render(name, edit, dirs, out_dir):
    out = Path(out_dir) / f'{name}.mp4'
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / 'headline.png'
        headline_png(edit['headline'], png)
        cmd, chains = ['ffmpeg', '-v', 'error', '-y'], []
        for i, (key, start, dur) in enumerate(edit['segments']):
            folder, fname = SOURCES[key]
            cmd += ['-ss', str(start), '-t', str(dur), '-i', str(Path(dirs[folder]) / fname)]
            crop = 'crop=ih*9/16:ih,' if key in WIDE else ''
            chains.append(f'[{i}:v]{crop}scale={W}:{H},setsar=1,fps={FPS},format=yuv420p[v{i}];'
                          f'[{i}:a]aresample=44100,aformat=channel_layouts=stereo,'
                          f'afade=t=in:d=0.15,afade=t=out:st={dur - 0.15}:d=0.15[a{i}]')
        n = len(edit['segments'])
        cmd += ['-i', str(png)]
        pairs = ''.join(f'[v{i}][a{i}]' for i in range(n))
        graph = ';'.join(chains) + f';{pairs}concat=n={n}:v=1:a=1[cv][ca];[cv][{n}:v]overlay=0:0[ov]'
        cmd += ['-filter_complex', graph, '-map', '[ov]', '-map', '[ca]',
                '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-c:a', 'aac', '-b:a', '160k',
                '-movflags', '+faststart', str(out)]
        subprocess.run(cmd, check=True)
    return out


if __name__ == '__main__':
    kylie_dir, craft_dir, out_dir = sys.argv[1:4]
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for name, edit in EDITS.items():
        print(render(name, edit, {'kylie': kylie_dir, 'craft': craft_dir}, out_dir))
