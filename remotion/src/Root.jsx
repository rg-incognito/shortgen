import React from 'react';
import {Composition} from 'remotion';
import {Captions} from './Captions';

// One composition. Size, fps and length come from the props JSON that
// src/remotion_captions.py writes (the caption band only, not the whole
// 1080x1920 canvas: ffmpeg overlays it at offset_y).
const DEFAULTS = {
  width: 1080, height: 520, fps: 30, duration_in_frames: 90, offset_y: 900,
  center_x: 530, center_y: 260, font_px: 90, stroke_px: 9, seam_s: 0, total_s: 3,
  colors: {base: '#FFFFFF', active: '#FFE600'},
  chunks: [{start: 0, end: 3, fit: 1, fade_out: false,
            lines: [[{text: 'PREVIEW', t: 0, color: null}, {text: 'CAPTIONS', t: 0.5, color: null}]]}],
};

export const Root = () => (
  <Composition
    id="Captions"
    component={Captions}
    defaultProps={DEFAULTS}
    width={DEFAULTS.width}
    height={DEFAULTS.height}
    fps={DEFAULTS.fps}
    durationInFrames={DEFAULTS.duration_in_frames}
    calculateMetadata={({props}) => ({
      width: props.width, height: props.height, fps: props.fps,
      durationInFrames: Math.max(1, props.duration_in_frames),
    })}
  />
);
