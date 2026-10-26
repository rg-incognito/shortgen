"""
Guest-change guard (BACKLOG P0-5, edit layer 'guest_cut').

A compilation source (LADbible's snack episode) can cut from one pair of guests to a
different pair mid-candidate: the Cena Short spliced two pairs, so its title was false.
This finds, for a candidate's clips, who is on screen per shot and rejects the candidate
when a cut separates two groups of people who never appear on the other side of it.

Faces: OpenCV YuNet (detection + 5 landmarks) and SFace (identity embedding), both from
opencv_zoo (YuNet MIT, SFace Apache-2.0), downloaded once next to face_landmarker.task.
Identities are greedy cosine clusters. To stay a guard, not a source of false rejects:
  - only clear faces count (>= _MIN_FACE_PX tall at _WIDTH px, score >= _MIN_SCORE);
  - a guest change needs >= 2 people on EACH side of the cut, none of them matching any
    face on the other side even at the looser _CROSS_SIM (an angle change of one person
    can split a cluster; two people switching at once is a different pair);
  - a person counts on a side when seen in >= 2 samples.
Cost: ~2 samples/s of candidate, a few seconds per candidate on CPU.
"""
import os
import re
import subprocess
import urllib.request

import cv2
import numpy as np

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
_MODELS = {
    'det': ('face_detection_yunet_2023mar.onnx',
            'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/'
            'face_detection_yunet_2023mar.onnx'),
    'rec': ('face_recognition_sface_2021dec.onnx',
            'https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/'
            'face_recognition_sface_2021dec.onnx'),
}
_WIDTH = 640
_FPS = 2.0
_MIN_FACE_PX = 48
_MIN_SCORE = 0.80
_SAME_SIM = 0.363          # SFace's published cosine threshold
_CROSS_SIM = 0.30          # looser: "never matches anyone on the other side"
_SCENE = 0.30              # ffmpeg scene score of a camera cut (as clipper._tail_cut)
_MIN_SEEN = 2


def _model(key):
    name, url = _MODELS[key]
    path = os.path.normpath(os.path.join(_ROOT, name))
    if not os.path.exists(path):
        print(f'  Downloading {name}...')
        tmp = path + '.part'
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, path)
    return path


class FaceIds:
    """YuNet + SFace with greedy identity clusters."""

    def __init__(self):
        self.det = cv2.FaceDetectorYN.create(_model('det'), '', (_WIDTH, 360), _MIN_SCORE, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(_model('rec'), '')
        self.centroids = []

    def faces(self, frame):
        """Feature vectors of the clear faces in one BGR frame."""
        h, w = frame.shape[:2]
        self.det.setInputSize((w, h))
        _, found = self.det.detect(frame)
        out = []
        for row in found if found is not None else []:
            if row[3] < _MIN_FACE_PX or row[-1] < _MIN_SCORE:
                continue
            crop = self.rec.alignCrop(frame, row)
            out.append(self.rec.feature(crop).flatten())
        return out

    def sim(self, a, b):
        return float(self.rec.match(a.reshape(1, -1), b.reshape(1, -1), cv2.FaceRecognizerSF_FR_COSINE))

    def identity(self, feat):
        """Cluster index of a face (a new cluster when it matches none)."""
        best, k = -1.0, None
        for i, c in enumerate(self.centroids):
            s = self.sim(feat, c[0] / max(1, c[1]))
            if s > best:
                best, k = s, i
        if k is not None and best >= _SAME_SIM:
            c = self.centroids[k]
            self.centroids[k] = (c[0] + feat, c[1] + 1)
            return k
        self.centroids.append((feat.copy(), 1))
        return len(self.centroids) - 1


def _frames(video, start, end, fps=_FPS, width=_WIDTH):
    """[(t, BGR frame)] sampled at fps over [start, end) (episode seconds)."""
    p = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                        'stream=width,height', '-of', 'csv=p=0', video], capture_output=True, text=True)
    try:
        sw, sh = [int(x) for x in p.stdout.strip().split(',')[:2]]
    except ValueError:
        sw, sh = 1280, 720
    h = max(2, int(round(width * sh / sw / 2)) * 2)
    p = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{start:.3f}', '-i', video, '-t', f'{end - start:.3f}',
                        '-an', '-vf', f'fps={fps},scale={width}:{h}', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
                        'pipe:1'], capture_output=True)
    size = width * h * 3
    return [(start + k / fps, np.frombuffer(p.stdout[k * size:(k + 1) * size], np.uint8).reshape(h, width, 3))
            for k in range(len(p.stdout) // size)]


def _cuts(video, start, end):
    """Camera cuts in [start, end] (episode seconds)."""
    p = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-ss', f'{start:.3f}', '-i', video,
                        '-t', f'{end - start:.3f}', '-an', '-vf',
                        f"scale=270:-2,select='gt(scene,{_SCENE})',showinfo", '-f', 'null', '-'],
                       capture_output=True, text=True, errors='replace')
    return [start + float(t) for t in re.findall(r'pts_time:([\d.]+)', p.stderr) if float(t) > 0.04]


def shots_with_people(video, clips, ids=None):
    """[(shot start in candidate seconds, {identity: samples})] across the candidate's clips."""
    ids = ids or FaceIds()
    shots, off = [], 0.0
    for c in clips:
        s, e = float(c['start_seconds']), float(c['end_seconds'])
        bounds = [s] + [t for t in _cuts(video, s, e) if s + 0.1 < t < e - 0.1] + [e]
        frames = _frames(video, s, e)
        for a, b in zip(bounds, bounds[1:]):
            seen = {}
            for t, fr in frames:
                if a <= t < b:
                    for f in ids.faces(fr):
                        k = ids.identity(f)
                        seen[k] = seen.get(k, 0) + 1
            shots.append((round(off + a - s, 2), seen))
        off += e - s
    return shots, ids


def find_guest_change(shots, ids):
    """(candidate seconds, before ids, after ids) of the first cut where the people change, or None."""
    for i in range(1, len(shots)):
        before, after = {}, {}
        for _, seen in shots[:i]:
            for k, n in seen.items():
                before[k] = before.get(k, 0) + n
        for _, seen in shots[i:]:
            for k, n in seen.items():
                after[k] = after.get(k, 0) + n
        b = {k for k, n in before.items() if n >= _MIN_SEEN}
        a = {k for k, n in after.items() if n >= _MIN_SEEN}
        if len(b) < 2 or len(a) < 2:
            continue
        cent = lambda k: ids.centroids[k][0] / max(1, ids.centroids[k][1])
        if any(ids.sim(cent(x), cent(y)) >= _CROSS_SIM for x in before for y in after):
            continue
        return shots[i][0], sorted(b), sorted(a)
    return None


def guest_change(video, clips):
    """A problem string when the candidate crosses a cut where the guests change, else None."""
    shots, ids = shots_with_people(video, clips)
    hit = find_guest_change(shots, ids)
    if not hit:
        return None
    t, b, a = hit
    return (f"crosses a cut where the guests change at {t:.1f}s ({len(b)} people before, {len(a)} different "
            f"people after)")
