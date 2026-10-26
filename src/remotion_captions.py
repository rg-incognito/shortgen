"""
Remotion caption layer (render['captions_engine'] = 'remotion').

The caption chunks, timings, keyword colours and shrink-to-fit come from
captioner._caption_chunks, the same layout the ASS captions use; Remotion
(remotion/, a pinned Node project) only animates them: words pop in as they
are spoken, the spoken word is yellow, thick black stroke. It renders a
transparent PNG sequence of the caption band (1080 x _BAND_H, centred on
caption_y), and captioner overlays it where the ass= captions would go.

Never fatal: no Node, no install, a failed or timed-out render all raise
RemotionUnavailable, and the caller burns the ASS captions instead.

Works the same on the GitHub runner (the workflow runs `npm ci` and
`remotion browser ensure` up front) and locally on Windows through
run_local.py: node is found with shutil.which, the Remotion CLI is run as
`node <remotion-cli.js>` (no npx / .cmd quoting), and a missing
node_modules is installed once with `npm ci` on first use. Remotion
downloads its headless Chromium into remotion/node_modules/.remotion on the
first render. Remotion is free for individuals and companies of up to 3 people.
"""
import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

REMOTION_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'remotion'))
FONT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'fonts'))
_CLI = os.path.join('node_modules', '@remotion', 'cli', 'remotion-cli.js')
_ENTRY = os.path.join('src', 'index.jsx')
_COMPOSITION = 'Captions'

_BAND_H = 520             # px: the rendered band (pop scale, 2-line full_subs and shadow fit)
_STROKE_PX = 9            # black stroke drawn under the fill (2x this, half covered by the fill)
_CAP_PX = 64              # Anton cap height, the same as the ASS presets (render_qa CAP_RANGE)
_ACTIVE = '#FFE600'       # the spoken word
_TIMEOUT_S = 420          # whole render; past it the ASS captions are burned instead
_INSTALL_TIMEOUT_S = 900
_COLOURS = {'&H0000E6FF&': '#FFE600', '&H006BE62E&': '#2EE66B', '&H00303BFF&': '#FF3B30'}

_install_tried = False
# A QA retry burns the same captions again: the band is kept for the run, keyed by its props.
_RENDERED = {}
_CACHE_ROOT = None


def _cache_dir(key):
    global _CACHE_ROOT
    if _CACHE_ROOT is None:
        _CACHE_ROOT = tempfile.mkdtemp(prefix='rm_band_')
        atexit.register(shutil.rmtree, _CACHE_ROOT, True)
    d = os.path.join(_CACHE_ROOT, key)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


class RemotionUnavailable(RuntimeError):
    """Remotion could not render this layer; the caller falls back to ASS captions."""


def _which(name):
    return shutil.which(name)


def node_path():
    return _which('node')


def cli_path(root=REMOTION_DIR):
    return os.path.join(root, _CLI)


def ensure_installed(root=REMOTION_DIR, timeout=_INSTALL_TIMEOUT_S):
    """
    Path of the Remotion CLI script, installing remotion/node_modules once with
    `npm ci` when it is missing (REMOTION_AUTO_INSTALL=0 turns that off).
    Raises RemotionUnavailable.
    """
    global _install_tried
    if not node_path():
        raise RemotionUnavailable('node not found on PATH')
    cli = cli_path(root)
    if os.path.exists(cli):
        return cli
    if os.environ.get('REMOTION_AUTO_INSTALL', '1') == '0':
        raise RemotionUnavailable(f'{cli} missing (run `npm ci` in {root})')
    if _install_tried:
        raise RemotionUnavailable('npm ci already failed in this run')
    _install_tried = True
    npm = _which('npm')                     # npm.cmd on Windows (PATHEXT)
    if not npm or not os.path.exists(os.path.join(root, 'package-lock.json')):
        raise RemotionUnavailable('npm or remotion/package-lock.json missing')
    print(f"  Remotion: first use, installing {root} (npm ci)...")
    t = time.time()
    try:
        p = subprocess.run([npm, 'ci', '--no-audit', '--no-fund'], cwd=root, capture_output=True,
                           text=True, errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RemotionUnavailable(f'npm ci failed ({exc})')
    if p.returncode != 0 or not os.path.exists(cli):
        raise RemotionUnavailable(f'npm ci failed: {(p.stderr or p.stdout).strip()[-300:]}')
    print(f"  Remotion: installed in {time.time() - t:.0f}s")
    return cli


def _hex(colour):
    return _COLOURS.get(colour) if colour else None


def _cap_font_px(cap_px):
    """CSS font-size (px) whose Anton cap height is cap_px (OS/2 capHeight / unitsPerEm)."""
    from captioner import _font_metrics, _font_path, _FONT_FILE
    upm, _, _, cap = _font_metrics(_font_path(_FONT_FILE))
    return round(cap_px * upm / cap, 1)


def build_props(chunks, total, fps, caption_y, seam=0.0, center_x=530, cap_px=_CAP_PX,
                lead=0.04, width=1080, band_h=_BAND_H, style='pop'):
    """
    Props JSON for the Captions composition. chunks: captioner._caption_chunks
    output (output seconds); fps: float. Words reveal `lead` s before they are
    spoken (never before their chunk is up). style 'edit' (edit layer, EDIT_SPEC R3):
    the chunk pops in (0.85 -> 1.0 over 80 ms, fade in over 60 ms) and only its keyword
    is coloured: no moving spoken-word highlight. Words keep their on-screen case ('disp').
    """
    offset_y = int(round(caption_y - band_h / 2))
    out = []
    for ch in chunks:
        lines = [[{'text': w.get('disp', w['text']), 't': round(max(ch['s'], w['start'] - lead), 4),
                   'color': _hex(c)}
                  for w, c in ln] for ln in ch['lines']]
        if lines and lines[0]:
            lines[0][0]['t'] = round(ch['s'], 4)          # the first word is up with its chunk
        out.append({'start': round(ch['s'], 4), 'end': round(ch['e'], 4), 'fit': round(ch['fx'] / 100, 3),
                    'fade_out': bool(ch['fade']), 'lines': lines})
    return {
        'width': int(width), 'height': int(band_h), 'fps': float(fps),
        'duration_in_frames': max(1, int(round(total * fps))),
        'offset_y': offset_y, 'center_x': int(center_x), 'center_y': int(caption_y - offset_y),
        'font_px': _cap_font_px(cap_px), 'stroke_px': _STROKE_PX,
        'seam_s': round(float(seam or 0), 4), 'total_s': round(float(total), 4),
        'colors': {'base': '#FFFFFF', 'active': _ACTIVE},
        'chunks': out,
        **({'style': 'edit'} if style == 'edit' else {}),
    }


def _kill_tree(proc):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def _sequence(out_dir):
    """(ffmpeg pattern, start number, frame count) of the rendered PNGs."""
    files = sorted(f for f in os.listdir(out_dir) if f.lower().endswith('.png'))
    if not files:
        raise RemotionUnavailable('render wrote no frames')
    m = re.match(r'^(.*?)(\d+)\.png$', files[0], re.I)
    if not m:
        raise RemotionUnavailable(f'unexpected frame name {files[0]}')
    pattern = os.path.join(out_dir, f"{m.group(1)}%0{len(m.group(2))}d.png")
    return pattern, int(m.group(2)), len(files)


def render(props, work, root=REMOTION_DIR, timeout=None):
    """
    Render the caption band to a PNG sequence (kept for the run, so a QA re-encode
    reuses it). Returns {'pattern', 'start', 'frames', 'offset_y', 'seconds'}
    (seconds 0.0 when reused). Raises RemotionUnavailable (no node, no install,
    render error, timeout, wrong frame count).
    """
    timeout = float(timeout or os.environ.get('REMOTION_TIMEOUT_S') or _TIMEOUT_S)
    blob = json.dumps(props, sort_keys=True)
    key = hashlib.sha1(blob.encode('utf-8')).hexdigest()[:16]
    hit = _RENDERED.get(key)
    if hit and os.path.isdir(os.path.dirname(hit['pattern'])):
        return dict(hit, seconds=0.0)
    cli = ensure_installed(root)
    props_path = os.path.join(work, 'rm_props.json')
    with open(props_path, 'w', encoding='utf-8') as f:
        f.write(blob)
    out_dir = _cache_dir(key)
    cmd = [node_path(), cli, 'render', _ENTRY, _COMPOSITION, out_dir,
           f'--props={props_path}', '--sequence', '--image-format=png',
           f'--public-dir={FONT_DIR}', '--log=error']
    if os.environ.get('REMOTION_CONCURRENCY'):
        cmd.append(f"--concurrency={os.environ['REMOTION_CONCURRENCY']}")
    t = time.time()
    try:
        proc = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors='replace')
    except OSError as exc:
        raise RemotionUnavailable(f'could not start node ({exc})')
    try:
        log, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        raise RemotionUnavailable(f'render timed out after {timeout:.0f}s')
    if proc.returncode != 0:
        raise RemotionUnavailable(f'render failed ({proc.returncode}): {(log or "").strip()[-400:]}')
    pattern, start, n = _sequence(out_dir)
    if abs(n - props['duration_in_frames']) > 1:
        raise RemotionUnavailable(f"{n} frames rendered, {props['duration_in_frames']} expected")
    out = {'pattern': pattern, 'start': start, 'frames': n, 'offset_y': props['offset_y'],
           'seconds': round(time.time() - t, 1)}
    _RENDERED[key] = out
    return dict(out)


if __name__ == '__main__':
    # quick self-check: `python src/remotion_captions.py` -> renders the default props
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    d = tempfile.mkdtemp(prefix='rm_check_')
    ch = [{'s': 0.0, 'e': 2.0, 'fx': 100, 'fade': True,
           'lines': [[({'text': 'REMOTION', 'start': 0.0}, None), ({'text': 'WORKS', 'start': 0.6}, None)]]}]
    print(render(build_props(ch, 2.0, 30.0, 960), d))
