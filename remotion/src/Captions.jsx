import React, {useState} from 'react';
import {
  AbsoluteFill, cancelRender, continueRender, delayRender, interpolate, spring, staticFile, useCurrentFrame,
  useVideoConfig,
} from 'remotion';

// Word-by-word captions on a transparent band. Python decides everything about
// the text (chunks, timings, keyword colours, shrink-to-fit); this file only
// animates it:
//   - a chunk pops in whole (scale 0.78 -> overshoot -> 1.0), so the line is
//     centred from its first frame and never re-centres;
//   - the word being spoken turns yellow with its own pop (1.0 -> 1.14, settles
//     at 1.10); a keyword keeps its colour (number / negative / name), the
//     rest are white;
//   - thick black stroke (painted under the fill) and a soft drop shadow;
//   - a chunk still up on the last frame fades out over the loop seam.
// Font: Anton from the repo's fonts/ folder (--public-dir), never a network font.

const FONT = 'AntonLocal';
let fontHandle = null;

const useLocalFont = () => {
  useState(() => {
    if (fontHandle !== null || typeof document === 'undefined') {
      return null;
    }
    fontHandle = delayRender('Anton font');
    const face = new FontFace(FONT, `url(${staticFile('Anton-Regular.ttf')}) format('truetype')`);
    face.load()
      .then((f) => {
        document.fonts.add(f);
        continueRender(fontHandle);
      })
      // a missing font fails the render (Python then falls back to the ASS captions)
      .catch((err) => cancelRender(err));
    return null;
  });
};

const POP = {damping: 11, stiffness: 260, mass: 0.6};

const Word = ({w, t, fps, active, base, activeColour, fontPx, strokePx}) => {
  const age = Math.max(0, (t - w.t) * fps);
  const lift = active ? spring({frame: age, fps, config: POP, durationInFrames: 6}) : 0;
  const scale = 1 + 0.10 * lift;
  const colour = active ? activeColour : (w.color || base);
  return (
    <span
      style={{
        display: 'inline-block',
        transform: `scale(${scale.toFixed(4)})`,
        transformOrigin: '50% 60%',
        color: colour,
        WebkitTextStroke: `${strokePx * 2}px #000`,
        paintOrder: 'stroke fill',
        textShadow: `0 ${Math.round(strokePx * 0.7)}px ${Math.round(strokePx * 0.9)}px rgba(0,0,0,0.55)`,
        margin: `0 ${Math.round(fontPx * 0.2)}px`,
        fontSize: fontPx,
        lineHeight: 1.12,
      }}
    >
      {w.text}
    </span>
  );
};

export const Captions = (props) => {
  useLocalFont();
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const t = frame / fps;
  const chunk = (props.chunks || []).find((c) => t >= c.start && t < c.end);
  if (!chunk) {
    return <AbsoluteFill style={{backgroundColor: 'transparent'}} />;
  }
  const words = chunk.lines.flat();
  // edit layer (props.style 'edit', EDIT_SPEC R3): no moving word highlight, only the chunk's
  // keyword keeps its colour; the chunk pops in 0.85 -> 1.0 over 80 ms (ease-out, no bounce)
  // and fades in over 60 ms.
  const edit = props.style === 'edit';
  // active word: the last one already revealed
  let active = -1;
  if (!edit) {
    words.forEach((w, i) => {
      if (t >= w.t) active = i;
    });
  }
  const since = Math.max(0, t - chunk.start);
  let pop;
  let opacity = 1;
  if (edit) {
    const p = Math.min(1, since / 0.08);
    pop = 0.85 + 0.15 * (1 - Math.pow(1 - p, 3));
    opacity = Math.min(1, since / 0.06);
  } else {
    const enter = spring({frame: since * fps, fps, config: POP, durationInFrames: 7});
    pop = interpolate(enter, [0, 1], [0.78, 1.0]);
  }
  if (chunk.fade_out && props.seam_s > 0) {
    opacity *= interpolate(t, [props.total_s - props.seam_s, props.total_s], [1, 0],
      {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  }
  let k = -1;
  return (
    <AbsoluteFill style={{backgroundColor: 'transparent'}}>
      <div
        style={{
          position: 'absolute', left: props.center_x, top: props.center_y,
          transform: `translate(-50%, -50%) scale(${(pop * chunk.fit).toFixed(4)}, ${pop.toFixed(4)})`,
          fontFamily: FONT, textAlign: 'center', whiteSpace: 'nowrap', opacity,
        }}
      >
        {chunk.lines.map((line, li) => (
          <div key={li}>
            {line.map((w) => {
              k += 1;
              return (
                <Word key={k} w={w} t={t} fps={fps} active={k === active}
                  base={props.colors.base} activeColour={props.colors.active}
                  fontPx={props.font_px} strokePx={props.stroke_px} />
              );
            })}
          </div>
        ))}
      </div>
    </AbsoluteFill>
  );
};
