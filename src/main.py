import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from drive_client import (download_video, upload_shorts, pick_video, pick_from_files,
                          move_to_archive, move_to_used_folder,
                          load_transcript_cache, save_transcript_cache)
from transcriber import transcribe_video
from analyzer import (analyze_candidates, generate_yt_metadata, build_sentences, build_candidate,
                      hook_problems, overlap_problems, rank_score, later_start, rubric_points, story_offset,
                      HOOK_CLASSES, PAYOFF_MAX_TAIL, GENRES)
from clipper import create_story_short, edited_duration
from sheet_tracker import SheetTracker, SKIP_PREFIX
from campaigns import (get_campaign, find_banned, find_transcript_banned, enforce_caption,
                       next_publish_time, publish_plan, window_publish_time, episode_campaign, SLOT_GUARD_MIN,
                       tidy_title, title_problems, clean_hashtags, header_problems, overlay_checks,
                       render_config, length_plan, story_max_seconds, layer_caps, rubric_gate, clip_text,
                       assign_variant, apply_variant, EXPERIMENTS, moment_hints, quote_problems, episode_guest)
from footage import download_from_release, sponsor_segments

try:   # render side (Coder B); absent until that branch is merged
    import textguard
except Exception:
    textguard = None

VIDEOS_FOLDER_ID  = os.environ.get('VIDEOS_FOLDER_ID',  '1bIFaNpfBy1Bp9R14T22EV-hMsYYrLeTS')
SHORTS_FOLDER_ID  = os.environ.get('SHORTS_FOLDER_ID',  '1kzFXWZQu8QQVtZKeAVvSgHcLaRFfK6tg')
ARCHIVE_FOLDER_ID = os.environ.get('ARCHIVE_FOLDER_ID', '1nHZCaWO_KvkV52TekkF8ecC5JtIlXqJj')
# A run whose best moment ranks below this uploads nothing: a weak Short stalls
# at the ~1K seed and uses up a slot (and a Whop submission) for nothing.
MIN_SCORE = float(os.environ.get('MIN_SCORE') or '7')
# This many score-gate skips in a row for one campaign fails the job, so the email fires.
MAX_CONSECUTIVE_SKIPS = 3


class WeakMomentSkip(Exception):
    """The best moment that passed every check still scored under MIN_SCORE."""
    kind = 'score gate'


class NoCandidateSkip(WeakMomentSkip, RuntimeError):
    """No candidate passed the campaign rules (recorded like a skip; still a RuntimeError)."""
    kind = 'no candidate passed'


def _sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _is_vertical(path):
    """True if the video's first stream is taller than wide (False when it cannot be probed)."""
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
             '-of', 'csv=p=0:s=x', str(path)],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        w, h = (int(v) for v in out.splitlines()[0].split('x')[:2])
        return h > w
    except Exception:
        return False


def _probe_duration(path):
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
        capture_output=True, text=True,
    )
    return float(out.stdout.strip()) if out.stdout.strip() else float('inf')


BURNED_TEXT_HOOK_S = 8.0    # no credits or source text while the viewer decides to stay
BURNED_TEXT_MAX_S  = 1.0    # ...and no more than this much of it anywhere
BURNED_TEXT_SOFT_ATTEMPT = 3   # from this attempt on, text outside the hook window only warns


# A render whose QA still finds burned-in text in 0-8 s: pick another moment this many
# times (each retry is a full render, so keep it small; the pre-pick scan makes it rare).
MAX_RESELECT = 1


def _burned_text_problems(story, video_path, attempt=1, profile=None):
    """
    Burned-in source text (opening credits, show titles, "previously on") in
    the candidate, via the render side's textguard.burned_text(video, start,
    end) -> [(t0, t1, band)] in episode seconds. Silently skipped when
    textguard is not installed or fails: it is a guard, never a reason to
    lose the run. Niche B (podcast / talk) only warns: those shows' own
    slides and lower thirds are legitimately flagged.
    """
    problems = _burned_text_scan(story, video_path, attempt)
    if problems and render_config(profile or {}).get('niche') == 'B':
        print(f"      Warning: {'; '.join(problems)} (kept: podcast/talk on-screen graphics)")
        return []
    return problems


def _burned_text_scan(story, video_path, attempt=1):
    if textguard is None or not video_path:
        return []
    hook_hits, total = [], 0.0
    for c in story['clips']:
        try:
            spans = textguard.burned_text(video_path, c['start_seconds'], c['end_seconds']) or []
        except Exception as e:
            print(f"      Burned-text check skipped: {e}")
            return []
        for sp in spans:
            t0, t1 = (sp.get('t0'), sp.get('t1')) if isinstance(sp, dict) else (sp[0], sp[1])
            t0, t1 = max(float(t0), c['start_seconds']), min(float(t1), c['end_seconds'])
            if t1 <= t0:
                continue
            total += t1 - t0
            off = story_offset(story['clips'], t0)
            if off is not None and off < BURNED_TEXT_HOOK_S:
                hook_hits.append(off)
    if hook_hits:
        return [f"burned-in text {min(hook_hits):.1f}s into the clip (first {BURNED_TEXT_HOOK_S:.0f}s)"]
    if total > BURNED_TEXT_MAX_S:
        if attempt < BURNED_TEXT_SOFT_ATTEMPT:
            return [f"burned-in text on screen for {total:.1f}s"]
        print(f"      Warning: burned-in text on screen for {total:.1f}s (allowed on attempt {attempt})")
    return []


def _apply_layers(story, all_words, profile):
    """
    Validate the originality layer: drop any overlay line that echoes the
    dialogue, uses a banned brief term or a vague word, keep annotations for
    reality/drama only. Returns hard problems (a header that echoes the
    dialogue). Sets story['_layers'] to the lines that survived.
    """
    if all_words:
        story['_clip_text'] = clip_text(story, all_words)   # metadata checks quotes against it
    # A brief can limit which overlay lines it allows (tonyhawk: header + closing line only).
    allowed = profile.get('overlay_fields')
    if allowed is not None:
        for field in ('context_line', 'closing_line'):
            if field not in allowed:
                story[field] = None
        if 'annotations' not in allowed:
            story['annotations'] = []
    bad = overlay_checks(story, all_words, profile)
    problems = []
    if 'header' in bad:
        why = bad.pop('header')
        if render_config(profile).get('header'):   # only matters when it is on screen
            problems.append(f"header echoes the dialogue ({why})")
    for field, why in bad.items():
        print(f"      ~ {story.get('storyline_title', '?')}: dropped {field} ({why})")
    for field in ('context_line', 'closing_line'):
        if field in bad:
            story[field] = None
    genre = profile.get('genre') or 'sitcom'
    story['annotations'] = [a for a in story.get('annotations') or []
                            if f"annotation@{a['t']:.1f}" not in bad and genre in ('reality', 'drama')]
    story['_layers'] = ([k for k in ('context_line', 'closing_line') if story.get(k)]
                        + (['annotations'] if story['annotations'] else []))
    return problems


def _v2_problems(story, profile, gate):
    """Rules for candidates in the v2 answer shape (hook_class, payoff_sentence, rubric, layers)."""
    problems = []
    hc = story.get('hook_class')
    if hc not in HOOK_CLASSES:
        problems.append(f"unknown hook_class {hc!r}")
    elif hc == 'SL':
        problems.append("opens on slow setup (hook_class SL)")
    if story.get('_payoff_tail', 0) > PAYOFF_MAX_TAIL:
        problems.append(f"ends {story['_payoff_tail']:.1f}s after the payoff (max {PAYOFF_MAX_TAIL:g}s)")
    if render_config(profile).get('overlays_allowed') and not story.get('_layers'):
        problems.append("no originality layer survived (closing/context line or annotations)")
    for field in profile.get('required_layers') or []:
        if field not in (story.get('_layers') or []):
            problems.append(f"required {field} missing or dropped")
    if story.get('_rubric') is not None and story['_rubric'] < gate:
        problems.append(f"rubric {story['_rubric']}/16 < {gate}")
    return problems


def _candidate_problems(story, sents, all_words, used, profile, video_path=None, attempt=1,
                        prior_headers=(), gate=None):
    """Every hard rule a candidate breaks (empty list = usable)."""
    lo = profile.get('min_seconds')
    hi = story.get('_max_seconds') or profile.get('max_seconds')
    dur = story['_edited_seconds']
    problems = []
    if lo and dur < lo:
        problems.append(f"too short after trimming ({dur:.1f}s < {lo}s)")
    if hi and dur > hi:
        problems.append(f"too long after trimming ({dur:.1f}s > {hi:g}s)")
    problems += hook_problems(story, sents, all_words)
    # Code-side dedupe: the LLM is told the used segments but not trusted with them.
    problems += overlap_problems(story['clips'], used)
    # The header is read in frame 0: no vague tease, no payoff word, no banned term, no reused opener.
    # A campaign that shows no header only keeps the banned-term check (the text still feeds titles/logs).
    if render_config(profile).get('header'):
        problems += header_problems(story, profile, prior_headers)
    else:
        banned = find_banned(story.get('header') or '', profile)
        if banned:
            problems.append(f"header uses banned words {banned}")
    ad_hits = find_transcript_banned(story, all_words, profile)
    if ad_hits:
        problems.append(f"clip audio looks like an ad read {ad_hits}")
    if story.get('_v2'):
        problems += _v2_problems(story, profile, rubric_gate(profile) if gate is None else gate)
    # Frame scan last and only for otherwise-usable candidates: it is the slow check.
    if not problems:
        problems += _burned_text_problems(story, video_path, attempt, profile)
    return problems


def _pick_story(segments, all_words, gemini_api_key, used_segments, used_storylines,
                campaign, profile, src_dur, attempts=3, video_path=None, prior_headers=(), hints=''):
    """
    Ask Gemini for 3-5 candidate moments as sentence ranges, cut them on whole
    sentences, rank them in code (0.4 hook + 0.3 payoff + 0.2 clarity + 0.1
    length fit, blended with the 16-point rubric) and return the best one
    that passes every campaign rule. A setup opener first tries a later start
    on the next question or conflict line. Rejected candidates join the
    avoid-list for the next attempt. Raises WeakMomentSkip if the best passing
    moment scores under MIN_SCORE.
    """
    sents = build_sentences(all_words, segments)
    lo, hi, target = length_plan(profile)
    limits = (lo, hi)
    genre = profile.get('genre') or GENRES.get(campaign, 'sitcom')
    min_score = profile.get('min_score', MIN_SCORE)
    gate = rubric_gate(profile)
    framing_cap, originality_cap = layer_caps(profile)
    # Same render config as the real render, so the length estimate matches it (tail, reaction cut).
    dur_kwargs = _accepted(edited_duration, render=render_config(profile), video_path=video_path,
                           wide_fallback=profile.get('wide_fallback', False))
    avoid = list(used_segments)
    best_weak = None
    hint_kw = {'hints': hints} if hints else {}   # episode notes (guest, flagged moments)
    for attempt in range(1, attempts + 1):
        raws = analyze_candidates(sents, gemini_api_key, used_segments=avoid,
                                  used_storylines=used_storylines, campaign=campaign, limits=limits,
                                  target=target, genre=genre, recent_headers=list(prior_headers), **hint_kw)
        ranked, used = [], list(avoid)   # candidates of one answer may overlap each other
        for raw in raws:
            story, problems = build_candidate(raw, sents, all_words)
            if not story:
                print(f"      - unusable candidate {raw.get('storyline_title', '?')}: {'; '.join(problems)}")
                continue
            if not render_config(profile).get('header'):
                problems = [p for p in problems if p != 'missing header']
            if story['_v2'] and (story.get('hook_class') == 'SL' or hook_problems(story, sents, all_words)):
                alt = later_start(story, sents, all_words)
                if alt and not hook_problems(alt, sents, all_words):
                    a, k = alt['_later_start']
                    print(f"      ~ {story['storyline_title']}: later start S{a} -> S{k} "
                          f"\"{alt['opening_line'][:50]}\" ({alt['hook_class']})")
                    story = alt
            for c in story['clips']:
                c['end_seconds'] = min(c['end_seconds'], src_dur - 0.05)
            dur = edited_duration(story, all_words, profile['silence_thresh'], **dur_kwargs)
            story['_edited_seconds'] = round(dur, 1)
            story['_max_seconds'] = story_max_seconds(story, profile)
            problems += _apply_layers(story, all_words, profile)
            if story['_v2']:
                o_cap = 1 if story['_layers'] == ['annotations'] else originality_cap
                story['_rubric'], story['_rubric_pts'] = rubric_points(
                    story, dur, framing_cap, min(o_cap, originality_cap), long_arc=story['_max_seconds'] > hi)
            story['score'] = rank_score(story, dur, target)
            problems += _candidate_problems(story, sents, all_words, used, profile, video_path=video_path,
                                            attempt=attempt, prior_headers=prior_headers, gate=gate)
            ranked.append((story, problems))
            avoid += [seg for seg in ((c['start_seconds'], c['end_seconds']) for c in story['clips'])
                      if seg not in avoid]
        ranked.sort(key=lambda sp: sp[0]['score'], reverse=True)

        print(f"      Attempt {attempt}: {len(ranked)} candidate(s)")
        for story, problems in ranked:
            verdict = 'ok' if not problems else 'rejected: ' + '; '.join(problems)
            tag = (f" {story.get('hook_class') or '?'} rubric {story['_rubric']}/16"
                   if story.get('_rubric') is not None else '')
            print(f"        [{story['score']:.1f}]{tag} {story['storyline_title']} "
                  f"({story['_edited_seconds']:.0f}s) \"{story['opening_line'][:60]}\" "
                  f"[{story.get('header', '')}] -> {verdict}")

        passing = [story for story, problems in ranked if not problems]
        if passing and passing[0]['score'] >= min_score:
            return passing[0]
        if passing and (not best_weak or passing[0]['score'] > best_weak['score']):
            best_weak = passing[0]

    if best_weak:
        err = WeakMomentSkip(f"best usable moment '{best_weak['storyline_title']}' scored "
                             f"{best_weak['score']:.1f} < {min_score:g}")
        # Every segment the LLM offered: logged as used so the next run moves on.
        err.segments = [seg for seg in avoid if seg not in used_segments]
        err.score = best_weak['score']
        raise err
    # Nothing passed: record it like a score-gate skip (offered segments marked used, the
    # 3-in-a-row alarm still fires) so the next run does not repeat the same work.
    err = NoCandidateSkip(f"No moment met the {campaign} campaign rules after {attempts} attempts")
    err.segments = [seg for seg in avoid if seg not in used_segments]
    err.score = 0
    raise err


def _make_metadata(story, episode_name, gemini_api_key, campaign, profile, prior_titles=(), attempts=4):
    """
    YouTube metadata that satisfies the campaign's caption rules and the title
    rules (no reused or "Wait…" titles, no spoilers; ideally under 60 chars,
    leading with a real name and not opening like this campaign's recent
    titles), or None if Gemini gives nothing. prior_titles: (episode, title).
    """
    all_titles = [t for _, t in prior_titles]
    campaign_titles = [t for ep, t in prior_titles if episode_campaign(ep) == campaign]
    fallback = vague_fallback = None
    guest = episode_guest(profile, episode_name)
    notes_kw = {'notes': f"Guest in this clip: {guest} (name them in the title or caption)."} if guest else {}
    finish = lambda m: clean_hashtags(enforce_caption(m, profile, episode_name), profile, episode_name)
    for attempt in range(1, attempts + 1):
        yt_meta = generate_yt_metadata(story, episode_name, gemini_api_key, campaign=campaign,
                                       title_names=profile.get('title_names'),
                                       recent_titles=campaign_titles[-30:], **notes_kw)
        if not yt_meta:
            break
        yt_meta['title'] = tidy_title(str(yt_meta.get('title', '')))
        text = ' '.join(str(yt_meta.get(k, '')) for k in ('title', 'description', 'tags', 'pinned_comment'))
        hard, soft = title_problems(yt_meta['title'], profile, all_titles, story.get('payoff_line', ''),
                                    campaign_titles, vague_hard=True)
        banned = find_banned(text, profile)
        if banned:
            hard.append(f"banned words {banned}")
        if profile.get('verbatim_quotes') and story.get('_clip_text') is not None:
            misquoted = quote_problems(text, story['_clip_text'])
            if misquoted:
                hard.append(f"quotes words not said in the clip {misquoted}")
        if not hard and not soft:
            return finish(yt_meta)
        print(f"      Metadata attempt {attempt} rejected ({yt_meta['title']!r}): {'; '.join(hard + soft)}")
        if not hard and not fallback:
            fallback = yt_meta
        # Metadata runs after the render: a title whose only hard issue is a vague word
        # is a last resort rather than a reason to throw the render away.
        if hard and all(h.startswith('vague words') for h in hard) and not vague_fallback:
            vague_fallback = yt_meta
    if fallback:
        print(f"      Using {fallback['title']!r} despite soft title issues")
        return finish(fallback)
    if vague_fallback:
        print(f"::warning title=Vague title::{episode_name}: using {vague_fallback['title']!r} "
              f"(every attempt used a vague word)")
        return finish(vague_fallback)
    if not yt_meta:
        return None
    raise RuntimeError(f"Could not generate {campaign} metadata that passes the title rules")


# Render QA (<out>.qa.json from the render side) -> tracker columns. The key
# names are matched loosely so either spelling the render side settles on lands.
_QA_KEYS = {
    'qa_lufs':         ('lufs', 'integrated_lufs', 'loudness_i', 'input_i', 'i'),
    'qa_true_peak':    ('true_peak', 'true_peak_db', 'tp', 'dbtp'),
    'qa_lra':          ('lra', 'loudness_range'),
    'qa_longest_shot': ('longest_shot', 'longest_shot_s'),
    'qa_tail_s':       ('tail_s', 'silence_after_last_word', 'tail_silence', 'tail'),
    'qa_burned_text':  ('burned_text', 'burned_text_0_8', 'burned_text_hook'),
    'qa_fail':         ('hard_fail', 'hard_fails', 'failed', 'fail'),
}


def _read_qa(short_path):
    """The render QA dict written next to the Short, or {} if there is none."""
    for p in (Path(str(short_path) + '.qa.json'), Path(short_path).with_suffix('.qa.json')):
        if p.exists():
            try:
                return json.loads(p.read_text(encoding='utf-8'))
            except (OSError, ValueError) as e:
                print(f"      QA file unreadable: {e}")
    return {}


def _qa_fields(qa):
    """Tracker fields from a (possibly nested) QA dict; the whole dict also goes in qa_json."""
    flat = {}

    def walk(d):
        for k, v in d.items():
            if isinstance(v, dict):
                walk(v)
            else:
                flat.setdefault(str(k).lower(), v)
    if isinstance(qa, dict):
        walk(qa)
    out = {}
    for field, keys in _QA_KEYS.items():
        for k in keys:
            if k in flat:
                v = flat[k]
                out[field] = json.dumps(v) if isinstance(v, (list, tuple)) else v
                break
    if qa:
        out['qa_json'] = json.dumps(qa, separators=(',', ':'), default=str)[:45000]
    return out


def _story_fields(story, first_comment):
    """Packaging and experiment fields logged per upload (sheet_tracker.EXTRA_HEADERS)."""
    variant = dict(story.get('variant') or {}, exp=story.get('_experiments') or [])
    return {
        'header':       story.get('header', ''),
        'hook_class':   story.get('hook_class') or '',
        'rubric':       (f"{story['_rubric']}/16 " + json.dumps(story.get('_rubric_pts') or {}, separators=(',', ':'))
                         if story.get('_rubric') is not None else ''),
        'variant':      json.dumps(variant, separators=(',', ':')),
        'context_line': story.get('context_line') or '',
        'closing_line': story.get('closing_line') or '',
        'annotations':  json.dumps(story.get('annotations') or [], ensure_ascii=False) if story.get('annotations') else '',
        'payoff_t':     story.get('payoff_t', ''),
        'first_comment': first_comment or '',
    }


def _accepted(fn, **kwargs):
    """The kwargs fn's signature accepts (so A and the render side merge in either order)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in kwargs.items() if k in params}


def _render_kwargs(render):
    """render= for create_story_short only when the render side accepts it."""
    return _accepted(create_story_short, render=render)


def _qa_burned_text(qa):
    """True if the render QA hard-failed on burned-in source text (fixable only by another moment)."""
    return any(str(f).startswith('burned_text') for f in (qa or {}).get('hard_fail') or [])


def _print_story(story):
    total_sec = story.get('_edited_seconds', story.get('_total_seconds', 0))
    print(f"      Story: [{story['score']}/10] ({story.get('format', 'story_recap')}) {story['storyline_title']}  (~{total_sec:.0f}s total)")
    print(f"      Summary: {story['storyline_summary']}")
    print(f"      Opens : \"{story['opening_line']}\" ({story.get('hook_class') or 'no hook_class'})")
    print(f"      Ends  : \"{story['payoff_line']}\" (payoff at {story.get('payoff_t', 0):.1f}s)")
    print(f"      Header: {story.get('header', '')}")
    for key in ('context_line', 'closing_line'):
        if story.get(key):
            print(f"      {key}: {story[key]}")
    for a in story.get('annotations') or []:
        print(f"      annotation @{a['t']:.1f}s: {a['text']}")
    if story.get('_rubric') is not None:
        print(f"      Rubric: {story['_rubric']}/16 {story.get('_rubric_pts')}")
    print(f"      {len(story['clips'])} clips:")
    for c in story['clips']:
        dur = c['end_seconds'] - c['start_seconds']
        print(f"        [{c['label']}] {c['start_seconds']:.1f}s-{c['end_seconds']:.1f}s ({dur:.0f}s) — {c['description']}")


def _select_and_render(tracker, campaign, profile, episode_name, run, segments, all_words, gemini_api_key,
                       avoid_segments, used_storylines, src_dur, video_path, work_dir, prior_headers=(),
                       pipeline=None):
    """
    Pick the best moment, assign its experiment arm and render it: (story,
    short_path, qa). When the render QA still finds burned-in source text in
    the hook window, that moment is set aside and the next one is picked (up
    to MAX_RESELECT times); other QA hard fails are retried inside the render.
    Raises WeakMomentSkip (score gate, or every pick had burned-in text).
    """
    in_campaign = lambda ep: episode_campaign(ep) == campaign
    rejected = []   # segments of renders that showed burned-in text
    hints = moment_hints(profile, episode_name)
    # A native 9:16 source (e.g. a brand's vertical recap cut) needs no 4:5 window crop.
    vertical = bool(profile.get('vertical_template')) and _is_vertical(video_path)
    if vertical:
        print(f"      Vertical source: template {profile['vertical_template']} (full frame)")
    for round_ in range(MAX_RESELECT + 1):
        try:
            story = _pick_story(segments, all_words, gemini_api_key, list(avoid_segments) + rejected,
                                used_storylines, campaign, profile, src_dur, video_path=str(video_path),
                                prior_headers=prior_headers, **({'hints': hints} if hints else {}))
        except WeakMomentSkip as e:
            e.segments = list(getattr(e, 'segments', None) or []) + rejected
            raise

        # Experiment arm (blocked randomisation per campaign; nothing runs unless EXPERIMENTS
        # or EXPERIMENTS_ON switches it on) and the matching render config.
        render = render_config(profile)
        try:
            prior_variants = tracker.get_prior_variants(in_campaign)
        except Exception as e:
            print(f"      Prior variants unavailable: {e}")
            prior_variants = []
        variant, story['_experiments'] = assign_variant(campaign, render, prior_variants,
                                                        seed=f"{episode_name}|{run}",
                                                        enabled=os.environ.get('EXPERIMENTS_ON'))
        if vertical and 'template' not in [EXPERIMENTS[e]['factor'] for e in story['_experiments']]:
            variant['template'] = profile['vertical_template']
        story['variant'] = variant
        apply_variant(story, variant)
        render = dict(render, template=variant['template'])
        render_kwargs = _render_kwargs(render)
        if render_kwargs and variant['template'] == 'A1':
            story['hook_emoji'] = ''   # the A1 header carries no emoji
        print(f"      Variant: {variant} experiments={story['_experiments'] or 'none'}"
              f"{'' if render_kwargs else ' (renderer has no render= yet: current look)'}")
        _print_story(story)

        shorts_dir = Path(work_dir) / ('shorts' if not round_ else f'shorts_{round_ + 1}')
        shorts_dir.mkdir()
        print("[4/5] Creating story compilation...")
        short_path = create_story_short(str(video_path), story, str(shorts_dir),
                                        episode_name, pipeline=pipeline,
                                        all_words=all_words,
                                        silence_thresh=profile['silence_thresh'],
                                        wide_fallback=profile.get('wide_fallback', False),
                                        **render_kwargs)
        qa = _read_qa(short_path)
        if qa:
            print(f"      Render QA: {_qa_fields(qa)}")
        if not _qa_burned_text(qa):
            other = [f for f in (qa or {}).get('hard_fail') or []]
            if other:
                # Uploaded anyway while QA is new (a false positive must not stop uploads).
                print(f"::warning title=Render QA::{episode_name} '{story['storyline_title']}' "
                      f"uploads with QA hard fails: {'; '.join(map(str, other))}")
            return story, short_path, qa
        rejected += [(c['start_seconds'], c['end_seconds']) for c in story['clips']]
        print(f"      Render QA found burned-in text in '{story['storyline_title']}' — "
              f"picking another moment ({round_ + 1}/{MAX_RESELECT})")
    skip = WeakMomentSkip(f"every pick ({MAX_RESELECT + 1}) showed burned-in text in the render QA")
    skip.segments, skip.score, skip.kind = rejected, story['score'], 'render QA burned text'
    raise skip


def _record_skip(tracker, campaign, episode_name, run, skip):
    """
    Log a score-gate skip as a run whose offered segments are used, so the
    next run moves on to new moments (or the next episode) instead of being
    offered the same weak ones forever. Exits non-zero after
    MAX_CONSECUTIVE_SKIPS skips in a row for this campaign so it gets noticed.
    """
    print(f"\n{'=' * 70}\nSKIPPED — NOTHING UPLOADED for {episode_name}: {skip}\n{'=' * 70}")
    tracker.log_clips(episode_name, run, [
        {'title': f"{SKIP_PREFIX}p{i + 1}", 'start_seconds': s, 'end_seconds': e,
         'score': skip.score, 'reason': f"{getattr(skip, 'kind', 'score gate')}: {skip}"}
        for i, (s, e) in enumerate(skip.segments or [(0.0, 0.0)])
    ])
    skips = tracker.consecutive_skips(lambda ep: episode_campaign(ep) == campaign)
    print(f"      Logged run {run} as skipped ({len(skip.segments)} segment(s) marked used); "
          f"{skips} skip(s) in a row for {campaign}")
    if skips >= MAX_CONSECUTIVE_SKIPS:
        print(f"::error title=Campaign stalled::{campaign}: {skips} runs in a row skipped by the score gate "
              f"(MIN_SCORE {MIN_SCORE:g}). Check the footage or lower MIN_SCORE.")
        sys.exit(1)
    print(f"::warning title=Run skipped::{campaign} {episode_name}: {skip}")


def main():
    campaign, profile = get_campaign(os.environ.get('CAMPAIGN'))
    max_runs = profile['max_runs']
    print(f"Campaign: {campaign}")

    service_account_info = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'].lstrip('﻿'))
    gemini_api_key        = os.environ['GEMINI_API_KEY']
    spreadsheet_id        = os.environ['SPREADSHEET_ID'].strip()
    whisper_model         = os.environ.get('WHISPER_MODEL', 'small')
    hf_token              = os.environ.get('HF_TOKEN', '').strip()

    video_file_id = os.environ.get('VIDEO_FILE_ID', '').strip()
    episode_name  = os.environ.get('EPISODE_NAME', '').strip()

    tracker = SheetTracker(service_account_info, spreadsheet_id)

    diarization_pipeline = None
    if hf_token:
        try:
            from reframer import load_diarization_pipeline
            diarization_pipeline = load_diarization_pipeline(hf_token)
            print('  Diarization pipeline loaded (speaker-aware tracking active)')
        except Exception as e:
            print(f'  Diarization pipeline unavailable: {e} — using face-only tracking')

    # A campaign with several daily slots gets one cron per slot, each passing
    # PUBLISH_SLOT=HH:MM (ET); without it the run serves whichever slot is next.
    pinned_slot = os.environ.get('PUBLISH_SLOT', '').strip() or None
    if pinned_slot:
        print(f"Publish slot pinned to {pinned_slot} ET")

    # GitHub cron runs can start hours late and the local fallback may also fire;
    # never put two Shorts of one campaign into the same publish slot.
    slot, nominal = publish_plan(profile, slot=pinned_slot)
    # Pinned: only Shorts for this slot (scheduled at or after it) count, not
    # an earlier slot's late publish.
    not_before = nominal if pinned_slot else None
    if slot and not video_file_id and tracker.slot_taken({ep for _, ep in profile.get('files', [])}, slot,
                                                             window_min=SLOT_GUARD_MIN,
                                                             not_before=not_before):
        print(f"{campaign}: a Short is already scheduled for the {slot} slot — nothing to do.")
        sys.exit(0)

    # Auto-pick if no file ID provided
    if not video_file_id:
        if profile['source'] in ('drive_files', 'release'):
            print(f"[0/5] Auto-picking {campaign} episode...")
            video_file_id, episode_name = pick_from_files(profile['files'], tracker, max_runs)
            if not video_file_id:
                print(f"Every {campaign} episode has reached {max_runs} runs — add footage or raise max_runs.")
                sys.exit(0)
        else:
            print("[0/5] Auto-picking video from videos/ folder...")
            video_file_id, episode_name = pick_video(VIDEOS_FOLDER_ID, service_account_info, tracker, max_runs=max_runs)
            if not video_file_id:
                print("No unprocessed videos in videos/ folder. Upload one and re-run.")
                sys.exit(0)
        print(f"      Picked: {episode_name}")
    elif not episode_name:
        episode_name = 'episode'

    episode_name = episode_name.replace(' ', '_')

    prior_runs       = tracker.get_run_count(episode_name)
    current_run      = prior_runs + 1
    used_segments    = tracker.get_used_segments(episode_name)
    used_storylines  = tracker.get_used_storylines(episode_name)

    print(f"      Episode : {episode_name}")
    print(f"      Run     : {current_run} of {max_runs}")
    print(f"      Avoiding: {len(used_segments)} previously used segment(s)")

    work_dir = Path(tempfile.mkdtemp(prefix='shortgen_'))
    try:
        video_path = work_dir / f"{episode_name}.mp4"

        print("[1/5] Downloading video...")
        if profile['source'] == 'release':
            download_from_release(profile['release_tag'], f"{video_file_id}.mp4", str(video_path))
        else:
            download_video(video_file_id, str(video_path), service_account_info)
        print(f"      {video_path.stat().st_size / 1e6:.1f} MB")

        # Long campaign episodes get clipped many times — transcribe each one once.
        cache_name = f"{episode_name}_{whisper_model}.json"
        cached = None
        try:
            cached = load_transcript_cache(cache_name, SHORTS_FOLDER_ID)
        except Exception as e:
            print(f"      Transcript cache unavailable: {e}")
        if cached:
            segments, all_words = cached['segments'], cached['words']
            print(f"[2/5] Using cached transcript ({len(segments)} segments, {len(all_words)} words)")
        else:
            print(f"[2/5] Transcribing with whisper-{whisper_model}...")
            segments, all_words = transcribe_video(str(video_path), model_size=whisper_model)
            print(f"      {len(segments)} segments, {len(all_words)} words transcribed")
            try:
                save_transcript_cache(cache_name, SHORTS_FOLDER_ID, segments, all_words)
            except Exception as e:
                print(f"      Transcript cache save skipped: {e}")

        print("[3/5] Analyzing for best story arc (Gemini)...")
        src_dur = _probe_duration(video_path)
        avoid_segments = list(used_segments)
        if profile.get('sponsorblock'):
            ads = sponsor_segments(video_file_id)
            print(f"      Avoiding {len(ads)} known ad-read segment(s) (SponsorBlock)")
            avoid_segments += ads
        if profile.get('avoid_first_s'):
            # Cheap prior next to the burned-text check: opening credits sit early in the episode.
            avoid_segments.append((0.0, float(profile['avoid_first_s'])))
            print(f"      Avoiding the first {profile['avoid_first_s']}s (opening credits)")
        in_campaign = lambda ep: episode_campaign(ep) == campaign
        try:
            prior_headers = tracker.get_prior_headers(in_campaign)
        except Exception as e:
            print(f"      Prior headers unavailable: {e}")
            prior_headers = []
        if textguard is None:
            print("      Burned-text check unavailable (textguard not installed)")
        try:
            story, short_path, qa = _select_and_render(
                tracker, campaign, profile, episode_name, current_run, segments, all_words, gemini_api_key,
                avoid_segments, used_storylines, src_dur, video_path, work_dir, prior_headers,
                diarization_pipeline)
        except WeakMomentSkip as e:
            _record_skip(tracker, campaign, episode_name, current_run, e)
            return

        print("      Generating YouTube metadata...")
        prior_titles = tracker.get_prior_titles()
        yt_meta = _make_metadata(story, episode_name, gemini_api_key, campaign, profile, prior_titles)
        if yt_meta:
            print(f"      YT title: {yt_meta.get('title', '')}")

        max_sec = story.get('_max_seconds') or profile.get('max_seconds')
        if profile.get('min_seconds') or max_sec:
            final_dur = _probe_duration(short_path)
            print(f"      Final length: {final_dur:.1f}s")
            if ((profile.get('min_seconds') and final_dur < profile['min_seconds'] - 1) or
                    (max_sec and final_dur > max_sec + 2)):
                raise RuntimeError(f"Rendered short is {final_dur:.1f}s — outside the {campaign} brief's limits")

        # Never re-upload: repeat uploads get treated as reused content, seed worse and burn a slot.
        file_sha1 = _sha1(short_path)
        if yt_meta:
            dup = tracker.find_prior_upload(yt_meta['title'], file_sha1)
            if dup:
                raise RuntimeError(f"Refusing to upload: {dup}")

        print("[5/5] Uploading to Drive...")
        uploaded = upload_shorts([short_path], SHORTS_FOLDER_ID, service_account_info)
        drive_file_id = uploaded[0][1] if uploaded else None

        _SERIES_PLAYLIST = {
            'ross_being_ross':         'Ross Being Ross',
            'joey_being_joey':         'Joey Being Joey',
            'chandler_being_chandler': 'Chandler Being Chandler',
            'phoebe_being_phoebe':     'Phoebe Being Phoebe',
            'monica_being_monica':     'Monica Being Monica',
            'rachel_being_rachel':     'Rachel Being Rachel',
            'relationship_drama':      'Relationship Drama',
            'gang_together':           'The Gang',
        }

        # Upload to YouTube as a scheduled post (requires YT_TOKEN_JSON secret)
        yt_video_id = None
        if yt_meta:
            # The closing line doubles as the first comment (posted after publish, only when enabled).
            first_comment = story.get('closing_line') or yt_meta.get('pinned_comment', '')
            # Row must exist before update_yt_url fills in scheduled_at / yt_url.
            tracker.log_posting_metadata(episode_name, current_run, story, yt_meta)
            try:
                tracker.log_story_fields(episode_name, current_run, story['storyline_title'],
                                         dict(_story_fields(story, first_comment), **_qa_fields(qa)))
            except Exception as e:
                print(f"::warning title=Tracker fields not written::{episode_name} run {current_run}: "
                      f"header/variant/QA columns skipped ({e})")
            from youtube_client import upload_scheduled, add_to_playlist
            # Profiles without slots publish ASAP, but still only in the US-evening window.
            publish_at = (next_publish_time(profile, slot=pinned_slot)
                          or window_publish_time(tracker.get_next_publish_slot()))
            print(f"[5b] Scheduling on YouTube → {publish_at}...")
            # Briefs that require a paid-partnership disclosure set paid_promotion.
            paid_kw = {'paid_promotion': True} if profile.get('paid_promotion') else {}
            yt_video_id = upload_scheduled(
                short_path,
                yt_meta['title'],
                yt_meta['description'],
                yt_meta['tags'],
                publish_at,
                **paid_kw,
            )
            print(f"      https://youtu.be/{yt_video_id}")
            tracker.update_yt_url(episode_name, current_run,
                                  story['storyline_title'], yt_video_id, publish_at, file_sha1=file_sha1)

            series_tag = story.get('series_tag', '')
            playlist_title = profile.get('playlist') or _SERIES_PLAYLIST.get(series_tag)
            if playlist_title:
                # YouTube often returns a transient 409 SERVICE_UNAVAILABLE right after upload.
                for attempt in (1, 2):
                    try:
                        add_to_playlist(yt_video_id, playlist_title)
                        print(f"      Added to playlist: {playlist_title}")
                        break
                    except Exception as pe:
                        if attempt == 2:
                            print(f"      Playlist add skipped: {pe}")
                        else:
                            time.sleep(20)

        # Move the uploaded short to shorts/used/
        if drive_file_id:
            try:
                move_to_used_folder(drive_file_id, SHORTS_FOLDER_ID)
                print("      Moved to shorts/used/")
            except Exception as e:
                print(f"      Move to used/ skipped: {e}")

        # Log each clip segment individually so future runs avoid them all
        clips_for_log = [
            {
                'title':         f"{story['storyline_title']}_p{i + 1}",
                'start_seconds': c['start_seconds'],
                'end_seconds':   c['end_seconds'],
                'score':         story['score'],
                'reason':        c['description'],
            }
            for i, c in enumerate(story['clips'])
        ]
        tracker.log_clips(episode_name, current_run, clips_for_log)
        print(f"      Logged run {current_run} to tracker sheet")

        if current_run >= max_runs and profile['archive']:
            print(f"      Run {current_run} = max runs reached — archiving video...")
            move_to_archive(video_file_id, ARCHIVE_FOLDER_ID, VIDEOS_FOLDER_ID, service_account_info)
            print(f"      Moved to archive/")

        print(f"\nDone! 1 story short in Drive → series_shorts/shorts/")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
