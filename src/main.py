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
                      trim_filler_opener, layer_line, HOOK_CLASSES, PAYOFF_MAX_TAIL, GENRES, length_trim)
from clipper import create_story_short, edited_duration
from sheet_tracker import SheetTracker, SKIP_PREFIX
from campaigns import (get_campaign, find_banned, find_transcript_banned, enforce_caption,
                       next_publish_time, publish_plan, window_publish_time, episode_campaign, SLOT_GUARD_MIN,
                       tidy_title, title_problems, clean_hashtags, header_problems, overlay_checks,
                       render_config, length_plan, story_max_seconds, layer_caps, rubric_gate, clip_text,
                       assign_variant, apply_variant, EXPERIMENTS, moment_hints, quote_problems, episode_guest,
                       fallback_layer_lines, layer_line_problem, layer_names, faithful_problems, _EMOJI,
                       clip_type_for_run, clip_type_hint, comments_allowed, whisper_hints, whisper_models,
                       edit_config, header_on, payoff_max_tail)
from footage import download_from_release, sponsor_segments
from scheduling import merge_bookings, slot_filled, start_blocked, choose_publish_time

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
# run_local.py --no-upload: pick, render, QA and write the metadata, then stop. Nothing goes to
# Drive, YouTube or the tracker sheet (it is only read); the Short, its QA, story and metadata are
# copied to NO_UPLOAD_DIR (default local_test/output/no_upload).
NO_UPLOAD = os.environ.get('NO_UPLOAD', '').strip().lower() in ('1', 'true', 'yes')


def _save_no_upload(short_path, story, yt_meta, campaign):
    out = Path(os.environ.get('NO_UPLOAD_DIR') or Path(__file__).parent.parent / 'local_test' / 'output' / 'no_upload')
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    dest = out / f"{campaign}_{stamp}.mp4"
    shutil.copy(short_path, dest)
    qa = Path(str(short_path) + '.qa.json')
    if qa.exists():
        shutil.copy(qa, str(dest) + '.qa.json')
    keep = {k: v for k, v in story.items() if not k.startswith('_sentences')}
    with open(str(dest) + '.story.json', 'w', encoding='utf-8') as f:
        json.dump(keep, f, indent=1, default=str, ensure_ascii=False)
    with open(str(dest) + '.meta.json', 'w', encoding='utf-8') as f:
        json.dump(yt_meta or {}, f, indent=1, ensure_ascii=False)
    print(f"\nNO UPLOAD: saved {dest} (+ .qa.json, .story.json, .meta.json). Nothing was uploaded or logged.")


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


# A render whose QA still finds burned-in text in 0-8 s, or whose frame is broken (black
# bars / static band) even after the render's blurred-fill retry: pick another moment
# this many times (each retry is a full render, so keep it small).
MAX_RESELECT = 1
# QA hard fails that break the frame (render_qa.LAYOUT_FAILS). Such a render is never uploaded.
LAYOUT_FAILS = ('black_bars', 'static_band')


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
    render = render_config(profile or {})
    if problems and (render.get('niche') == 'B' or render.get('burned_text_ok')):
        # burned_text_ok: a brand's own show graphics (milkxp's labels and lesson cards) are content
        print(f"      Warning: {'; '.join(problems)} (kept: the show's own on-screen graphics)")
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


def _no_emoji(text):
    return ' '.join(_EMOJI.sub('', str(text or '')).split())


def _apply_layers(story, all_words, profile):
    """
    Validate the originality layer: drop any overlay line that echoes the
    dialogue, uses a banned brief term or a vague word, keep annotations for
    reality/drama only. Returns hard problems (a header that echoes the
    dialogue). Sets story['_layers'] to the lines that survived.
    """
    # On-screen lines are drawn in Montserrat: an emoji renders as a monochrome outline glyph
    for field in ('context_line', 'closing_line'):
        if story.get(field):
            story[field] = _no_emoji(story[field]) or None
    if all_words:
        story['_clip_text'] = clip_text(story, all_words)   # metadata checks quotes against it
        # words said before the punchline sentence: setup, not spoiler (header_problems)
        ps = story.get('_payoff_start')
        if ps is not None:
            story['_setup_text'] = ' '.join(w['word'] for c in story['clips'] for w in all_words
                                            if c['start_seconds'] <= w['start'] < min(c['end_seconds'], ps))
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
        if header_on(profile):   # only matters when it is on screen
            problems.append(f"header echoes the dialogue ({why})")
    for field, why in bad.items():
        print(f"      ~ {story.get('storyline_title', '?')}: dropped {field} ({why})")
    for field in ('context_line', 'closing_line'):
        if field in bad:
            # the follow-up ask is told not to repeat it
            story.setdefault('_layer_tried', {}).setdefault(field, []).append(story[field])
            story[field] = None
    genre = profile.get('genre') or 'sitcom'
    story['annotations'] = [a for a in story.get('annotations') or []
                            if f"annotation@{a['t']:.1f}" not in bad and genre in ('reality', 'drama')]
    story['_layers'] = _layer_fields(story)
    return problems


def _layer_fields(story):
    return ([k for k in ('context_line', 'closing_line') if story.get(k)]
            + (['annotations'] if story.get('annotations') else []))


def _layer_needs(story, profile):
    """
    Originality lines this candidate still needs: any field the brief requires
    (required_layers), or, when the campaign shows overlays and none survived
    (and the layer is not optional), one line of the genre's primary kind.
    """
    have = story.get('_layers') or []
    allowed = profile.get('overlay_fields')
    allowed = [f for f in ('context_line', 'closing_line') if allowed is None or f in allowed]
    need = [f for f in profile.get('required_layers') or [] if f in allowed and f not in have]
    if (not need and not have and allowed and render_config(profile).get('overlays_allowed')
            and not profile.get('layers_optional')):
        talk = (profile.get('genre') or 'sitcom') in ('podcast', 'talk')
        need = ['context_line' if talk and 'context_line' in allowed else
                'closing_line' if 'closing_line' in allowed else allowed[0]]
    return need


def _fill_layers(story, all_words, profile, ask=None):
    """
    Fill an originality line the LLM omitted or that was dropped, instead of
    throwing a good candidate away. Without ask: safe lines derived from the
    LLM's own fields (stake_line / header / summary as a context line; a genre
    question as a closing line). With ask(field, rejected) -> line: the one
    cheap follow-up to Gemini. Every line passes the same overlay checks.
    Returns the fields filled.
    """
    need = _layer_needs(story, profile)
    if not need:
        return []
    transcript, names = layer_names(story, all_words, profile)
    filled = []
    for field in need:
        tried = story.setdefault('_layer_tried', {}).setdefault(field, [])
        options = ([_no_emoji(ask(field, [t for t in tried if t]))] if ask
                   else fallback_layer_lines(story, field, profile))
        for text in options:
            if not text or text in tried:
                continue
            why = layer_line_problem(field, text, transcript, names, profile)
            if why:
                tried.append(text)
                continue
            story[field] = text
            src = 'follow-up ask' if ask else 'derived'
            story.setdefault('_layer_fallback', {})[field] = src
            print(f"      ~ {story.get('storyline_title', '?')}: {field} filled ({src}): {text!r}")
            filled.append(field)
            break
    story['_layers'] = _layer_fields(story)
    return filled


def _is_layer_problem(p):
    return p.startswith('no originality layer survived') or (p.startswith('required ') and 'missing or dropped' in p)


def _trims_filler(profile):
    """Podcast / talk: a leading filler word is trimmed off the opener instead of rejecting it."""
    return profile.get('trim_filler', render_config(profile).get('niche') == 'B')


def _v2_problems(story, profile, gate):
    """Rules for candidates in the v2 answer shape (hook_class, payoff_sentence, rubric, layers)."""
    problems = []
    hc = story.get('hook_class')
    if hc not in HOOK_CLASSES:
        problems.append(f"unknown hook_class {hc!r}")
    elif hc == 'SL':
        problems.append("opens on slow setup (hook_class SL)")
    max_tail = payoff_max_tail(profile)
    if story.get('_payoff_tail', 0) > max_tail:
        problems.append(f"ends {story['_payoff_tail']:.1f}s after the payoff (max {max_tail:g}s)")
    if (render_config(profile).get('overlays_allowed') and not story.get('_layers')
            and not profile.get('layers_optional')):
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
    problems += _hook_problems(story, sents, all_words, profile)
    # Code-side dedupe: the LLM is told the used segments but not trusted with them.
    problems += overlap_problems(story['clips'], used)
    # The header is read in frame 0: no vague tease, no payoff word, no banned term, no reused opener.
    # A campaign that shows no header only keeps the banned-term check (the text still feeds titles/logs).
    if header_on(profile):
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
    if not problems and edit_config(profile).get('guest_cut'):
        problems += _guest_cut_problems(story, video_path)
    return problems


# Edit-layer rules that may be relaxed so a campaign never ends a run with zero Shorts: the
# P0-3 opener rejects, P0-5 guest change, the on-screen headline rules (the headline is then
# hidden) and the P0-1 window, down to the pre-edit-layer limits. Everything else stays hard.
_SOFT_PREFIXES = ('opens mid-sentence', 'opens on an outro', 'opens on an abstract', 'abstract opener',
                  'opener leans', 'crosses a cut where the guests change')


def _soft_problem(p, profile):
    """(soft?, hides the headline?) for one candidate problem string."""
    import re as _re
    from campaigns import NICHE_LENGTH
    if p.startswith(_SOFT_PREFIXES):
        return True, False
    if p.startswith('header ') and 'banned' not in p:
        # soft only where the header is on screen because of the edit layer (A1 showed it before)
        legacy = bool(render_config(profile).get('header'))
        return (not legacy or 'describes the clip' in p), not legacy
    m = _re.match(r'too short after trimming \(([\d.]+)s', p)
    if m:
        return float(m.group(1)) >= float(profile.get('brief_min_seconds') or profile.get('min_seconds') or 0), False
    m = _re.match(r'too long after trimming \(([\d.]+)s', p)
    if m:
        niche = NICHE_LENGTH[render_config(profile).get('niche', 'A')]['max']
        return float(m.group(1)) <= min(profile.get('max_seconds') or niche, niche), False
    m = _re.match(r'ends ([\d.]+)s after the payoff', p)
    if m:
        return float(m.group(1)) <= PAYOFF_MAX_TAIL, False
    return False, False


def _edit_fallback(candidates, profile, min_score):
    """
    The best candidate whose only problems are edit-layer soft rules (and that clears the score
    gate), marked '_relaxed' (and '_hide_headline' when its header broke an on-screen rule), or None.
    """
    best = None
    for story, problems in candidates:
        if not problems or story['score'] < min_score:
            continue
        judged = [_soft_problem(p, profile) for p in problems]
        if not all(s for s, _ in judged):
            continue
        if best is None or story['score'] > best[0]['score']:
            best = (story, problems, any(h for _, h in judged))
    if not best:
        return None
    story, problems, hide = best
    story['_relaxed'] = list(problems)
    if hide:
        story['_hide_headline'] = True
    print(f"::warning title=Edit layer fallback::'{story['storyline_title']}' [{story['score']:.1f}] picked with "
          f"edit-layer rules relaxed ({'; '.join(problems)})" + (' — headline hidden' if hide else ''))
    return story


def _hook_problems(story, sents, all_words, profile):
    """hook_problems with the edit layer's deterministic opener rejects (P0-3) when it is on."""
    return hook_problems(story, sents, all_words, strict=bool(edit_config(profile).get('hook_rejects')),
                         genre=profile.get('genre'))


def _guest_cut_problems(story, video_path):
    """P0-5: the candidate crosses a source cut where the people on screen change (faceid). A guard:
    any failure of the face pass is a warning, never a reason to lose the run."""
    if not video_path:
        return []
    try:
        import faceid
        hit = faceid.guest_change(video_path, story['clips'])
    except Exception as e:
        print(f"      Guest-change check skipped: {e}")
        return []
    return [hit] if hit else []


def _pick_story(segments, all_words, gemini_api_key, used_segments, used_storylines,
                campaign, profile, src_dur, attempts=3, video_path=None, prior_headers=(), hints='',
                want_type=None):
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
        if attempt == 1:
            everything = []                  # every candidate of every attempt (edit-layer fallback)
        ranked, used = [], list(avoid)   # candidates of one answer may overlap each other
        for raw in raws:
            story, problems = build_candidate(raw, sents, all_words, max_tail=payoff_max_tail(profile))
            if not story:
                print(f"      - unusable candidate {raw.get('storyline_title', '?')}: {'; '.join(problems)}")
                continue
            if not header_on(profile):
                problems = [p for p in problems if p != 'missing header']
            if _trims_filler(profile):
                trimmed = trim_filler_opener(story, sents, all_words)
                if trimmed:
                    print(f"      ~ {story['storyline_title']}: trimmed filler \"{trimmed['_filler_trim']}\" -> "
                          f"\"{trimmed['opening_line'][:50]}\"")
                    story = trimmed
            if story['_v2'] and (story.get('hook_class') == 'SL'
                                 or _hook_problems(story, sents, all_words, profile)):
                alt = later_start(story, sents, all_words)
                if alt and _trims_filler(profile):
                    alt = trim_filler_opener(alt, sents, all_words) or alt
                if alt and not _hook_problems(alt, sents, all_words, profile):
                    a, k = alt['_later_start']
                    print(f"      ~ {story['storyline_title']}: later start S{a} -> S{k} "
                          f"\"{alt['opening_line'][:50]}\" ({alt['hook_class']})")
                    story = alt
            for c in story['clips']:
                c['end_seconds'] = min(c['end_seconds'], src_dur - 0.05)
            dur = edited_duration(story, all_words, profile['silence_thresh'], **dur_kwargs)
            if edit_config(profile).get('length') and dur > target[1] + 0.05:
                # P0-1: over the target -> start later on a clean sentence (the payoff stays last):
                # the first start that lands in the target window, else the first under the cap
                lo_ = profile.get('min_seconds') or 0.0
                cut = None
                for hi_ in (target[1], story_max_seconds(story, profile)):
                    def fits(s, hi_=hi_):
                        s['_dur_try'] = edited_duration(s, all_words, profile['silence_thresh'], **dur_kwargs)
                        return (lo_ <= s['_dur_try'] <= hi_ + 0.05
                                and not _hook_problems(s, sents, all_words, profile))
                    if dur > hi_ + 0.05:
                        cut = length_trim(story, sents, all_words, fits)
                    if cut:
                        break
                if cut:
                    a_, k_ = cut['_length_trim']
                    print(f"      ~ {story['storyline_title']}: {dur:.1f}s > {hi_:g}s, starts later S{a_} -> S{k_} "
                          f"\"{cut['opening_line'][:50]}\" ({cut['_dur_try']:.1f}s)")
                    story, dur = cut, cut['_dur_try']
            story['_edited_seconds'] = round(dur, 1)
            story['_max_seconds'] = story_max_seconds(story, profile)
            problems += _apply_layers(story, all_words, profile)
            _fill_layers(story, all_words, profile)
            if story['_v2']:
                o_cap = 1 if story['_layers'] == ['annotations'] else originality_cap
                story['_rubric'], story['_rubric_pts'] = rubric_points(
                    story, dur, framing_cap, min(o_cap, originality_cap), long_arc=story['_max_seconds'] > hi)
            story['score'] = rank_score(story, dur, target)
            base = list(problems)
            problems = base + _candidate_problems(story, sents, all_words, used, profile, video_path=video_path,
                                                  attempt=attempt, prior_headers=prior_headers, gate=gate)
            # Only a missing originality line stands between this candidate and a pass:
            # one cheap follow-up ask before it is thrown away.
            if problems and gemini_api_key and all(_is_layer_problem(p) for p in problems):
                ask = lambda field, tried, s=story: layer_line(s, field, s.get('_clip_text') or '', gemini_api_key,
                                                               campaign, tried)
                if _fill_layers(story, all_words, profile, ask=ask):
                    problems = base + _candidate_problems(story, sents, all_words, used, profile,
                                                          video_path=video_path, attempt=attempt,
                                                          prior_headers=prior_headers, gate=gate)
            ranked.append((story, problems))
            avoid += [seg for seg in ((c['start_seconds'], c['end_seconds']) for c in story['clips'])
                      if seg not in avoid]
        ranked.sort(key=lambda sp: sp[0]['score'], reverse=True)
        everything += ranked

        print(f"      Attempt {attempt}: {len(ranked)} candidate(s)")
        for story, problems in ranked:
            verdict = 'ok' if not problems else 'rejected: ' + '; '.join(problems)
            tag = (f" {story.get('hook_class') or '?'} rubric {story['_rubric']}/16"
                   if story.get('_rubric') is not None else '')
            print(f"        [{story['score']:.1f}]{tag} {story['storyline_title']} "
                  f"({story['_edited_seconds']:.0f}s) \"{story['opening_line'][:60]}\" "
                  f"[{story.get('header', '')}] -> {verdict}")

        passing = [story for story, problems in ranked if not problems]
        # A brief with several clip kinds (milkxp): this run's kind wins when one passes the gate.
        if want_type:
            typed = [s for s in passing if str(s.get('clip_type') or '').strip().lower() == want_type
                     and s['score'] >= min_score]
            if typed:
                if typed[0] is not passing[0]:
                    print(f"      Clip type {want_type}: '{typed[0]['storyline_title']}' "
                          f"[{typed[0]['score']:.1f}] over '{passing[0]['storyline_title']}'")
                return typed[0]
        if passing and passing[0]['score'] >= min_score:
            return passing[0]
        if passing and (not best_weak or passing[0]['score'] > best_weak['score']):
            best_weak = passing[0]

    if not best_weak and any(edit_config(profile).get(k) for k in ('hook_rejects', 'guest_cut', 'headline', 'length')):
        fb = _edit_fallback(everything, profile, min_score)
        if fb:
            return fb
    if best_weak:
        err = WeakMomentSkip(f"best usable moment '{best_weak['storyline_title']}' scored "
                             f"{best_weak['score']:.1f} < {min_score:g}")
        # Every segment the LLM offered: logged as used so the next run moves on.
        err.segments = [seg for seg in avoid if seg not in used_segments]
        err.score = best_weak['score']
        raise err
    # Nothing passed: record the run so the 3-in-a-row alarm still fires, but leave the offered
    # segments free. They failed a rule (length, header, missing layer...), not on quality, and
    # marking them used burned a whole short source (the 117 s Tony Hawk finale) in one run.
    err = NoCandidateSkip(f"No moment met the {campaign} campaign rules after {attempts} attempts")
    err.segments = []
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
    note = f"Guest in this clip: {guest} (name them in the title or caption)." if guest else ''
    finish = lambda m: clean_hashtags(enforce_caption(m, profile, episode_name), profile, episode_name)
    # Title faithfulness: the clip's own words go into the prompt, and a title that adds a
    # person or role the clip never mentions ("her boss") is rejected and retried.
    transcript = story.get('_clip_text')
    known = ' '.join([guest or ''] + list(profile.get('title_names') or []) + list(profile.get('cast_names') or []))
    feedback = ''
    for attempt in range(1, attempts + 1):
        notes = ' '.join(x for x in (note, feedback) if x)
        yt_meta = generate_yt_metadata(story, episode_name, gemini_api_key, campaign=campaign,
                                       title_names=profile.get('title_names'),
                                       recent_titles=campaign_titles[-30:],
                                       **({'notes': notes} if notes else {}),
                                       **({'transcript': transcript} if transcript else {}))
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
        hard += faithful_problems(yt_meta, transcript, profile, known)
        yt_meta.pop('title_check', None)
        if not hard and not soft:
            return finish(yt_meta)
        print(f"      Metadata attempt {attempt} rejected ({yt_meta['title']!r}): {'; '.join(hard + soft)}")
        if hard:
            feedback = f'A previous title was rejected ({"; ".join(hard)}): write a different one.'
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


# Edit-layer hook fails (P0-3, render_qa.HOOK_FAILS): no face in the first 1.5 s, or source text
# under our captions in 0-3 s. Only another moment fixes them: that render is never uploaded.
HOOK_FAILS = ('no_face_open', 'text_under_captions')


def _qa_hook_failed(qa):
    return [str(f) for f in (qa or {}).get('hard_fail') or [] if str(f).split(':')[0] in HOOK_FAILS]


def _qa_layout_broken(qa):
    """The render QA's layout-breaking hard fails (black bars, static band) — never uploaded."""
    return [str(f) for f in (qa or {}).get('hard_fail') or [] if str(f).split(':')[0] in LAYOUT_FAILS]


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
    the hook window, or a broken frame (black bars / static band) that the
    render's one blurred-fill retry did not fix, that moment is set aside and
    the next one is picked (up to MAX_RESELECT times); other QA hard fails are
    retried inside the render and upload with a warning.

    CI cost bound per run: at most MAX_RESELECT + 1 (= 2) full renders plus ONE
    QA retry in total (the safe caption re-encode, or the layout re-render that
    reuses the face analysis); qa_retry=False tells later renders it is spent.
    Raises WeakMomentSkip (score gate, or every pick failed the render QA).
    """
    in_campaign = lambda ep: episode_campaign(ep) == campaign
    rejected = []   # segments of renders that showed burned-in text or a broken frame
    qa_retry = True  # the run's one QA retry (see the docstring) is still available
    reasons = []
    hints = moment_hints(profile, episode_name)
    # Briefs that want several kinds of clips: ask for all of them, prefer this run's kind.
    want_type = clip_type_for_run(profile, run)
    if want_type:
        hints = '\n\n'.join(h for h in (hints, clip_type_hint(profile, run)) if h)
        print(f"      Clip type wanted this run: {want_type}")
    # A native 9:16 source (e.g. a brand's vertical recap cut) needs no 4:5 window crop.
    vertical = bool(profile.get('vertical_template')) and _is_vertical(video_path)
    if vertical:
        print(f"      Vertical source: template {profile['vertical_template']} (full frame)")
    for round_ in range(MAX_RESELECT + 1):
        try:
            story = _pick_story(segments, all_words, gemini_api_key, list(avoid_segments) + rejected,
                                used_storylines, campaign, profile, src_dur, video_path=str(video_path),
                                prior_headers=prior_headers, **({'hints': hints} if hints else {}),
                                **({'want_type': want_type} if want_type else {}))
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
        if story.get('_hide_headline'):
            # edit-layer fallback: this header broke an on-screen rule, so no headline on this Short
            render = dict(render, edit=dict(edit_config(render), headline=False))
        render_kwargs = dict(_render_kwargs(render), **_accepted(create_story_short, qa_retry=qa_retry))
        if render_kwargs and variant['template'] == 'A1' and not edit_config(render).get('headline'):
            story['hook_emoji'] = ''   # the A1 header carries no emoji (the edit headline may carry one)
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
        if (qa or {}).get('qa_retry'):
            qa_retry = False
        broken = _qa_layout_broken(qa)
        hook = _qa_hook_failed(qa)
        if not _qa_burned_text(qa) and not broken and not hook:
            other = [f for f in (qa or {}).get('hard_fail') or []]
            if other:
                # Uploaded anyway while QA is new (a false positive must not stop uploads).
                print(f"::warning title=Render QA::{episode_name} '{story['storyline_title']}' "
                      f"uploads with QA hard fails: {'; '.join(map(str, other))}")
            return story, short_path, qa
        rejected += [(c['start_seconds'], c['end_seconds']) for c in story['clips']]
        why = ('burned-in text' if _qa_burned_text(qa) else f"a broken frame ({'; '.join(broken)})" if broken
               else f"a weak opening frame ({'; '.join(hook)})")
        reasons.append('burned text' if _qa_burned_text(qa) else 'layout' if broken else 'hook')
        print(f"::warning title=Render QA::{episode_name} '{story['storyline_title']}' NOT uploaded: "
              f"render QA found {why}")
        if round_ < MAX_RESELECT:
            print(f"      Picking another moment ({round_ + 1}/{MAX_RESELECT})")
    what = ('showed burned-in text' if set(reasons) == {'burned text'} else
            'had a broken frame (black bars / static band)' if set(reasons) == {'layout'} else
            'opened without a face or with source text under the captions' if set(reasons) == {'hook'} else
            'failed the render QA (burned-in text, a broken frame or a weak opening frame)')
    skip = WeakMomentSkip(f"every pick ({MAX_RESELECT + 1}) {what} in the render QA")
    skip.segments, skip.score = rejected, story['score']
    skip.kind = ('render QA burned text' if set(reasons) == {'burned text'} else
                 'render QA layout' if set(reasons) == {'layout'} else
                 'render QA hook' if set(reasons) == {'hook'} else 'render QA')
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


def _bookings(tracker):
    """Every publish time of ours (scheduling.merge_bookings): YouTube API + tracker sheet."""
    yt_items = None
    try:
        from youtube_client import own_schedule
        yt_items = own_schedule()
    except Exception as e:
        print(f"::warning title=YouTube schedule unavailable::{e} (slot checks use the tracker sheet only)")
    try:
        rows = tracker.get_bookings()
    except Exception as e:
        print(f"::warning title=Tracker bookings unavailable::{e}")
        rows = []
    return merge_bookings(yt_items, rows)


def _transcript_cache_names(episode_name, models, hot):
    """Cache file names to try, best first. Hotword transcripts get their own key; a campaign
    that keeps its old model may reuse its old (hotword-free) transcript."""
    tag = ('_hw' + hashlib.sha1(hot.encode('utf-8')).hexdigest()[:6]) if hot else ''
    names = [f"{episode_name}_{m}{tag}.json" for m in models]
    if tag and len(models) == 1:
        names.append(f"{episode_name}_{models[0]}.json")
    return names, tag


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
    # The check reads YouTube (every upload on the channel) and the tracker sheet; it runs again
    # right before the upload, since a late GitHub run and the local fallback can render at once.
    if slot and not video_file_id:
        blocked = start_blocked(campaign, profile, nominal, _bookings(tracker), pinned=bool(pinned_slot))
        if blocked:
            if NO_UPLOAD:
                print(f"{campaign} {nominal} slot: {blocked} — rendering anyway (no-upload run)")
            else:
                print(f"{campaign} {nominal} slot: {blocked} — nothing to do.")
                sys.exit(0)

    diarization_pipeline = None
    if hf_token:
        try:
            from reframer import load_diarization_pipeline
            diarization_pipeline = load_diarization_pipeline(hf_token)
            print('  Diarization pipeline loaded (speaker-aware tracking active)')
        except Exception as e:
            print(f'  Diarization pipeline unavailable: {e} — using face-only tracking')

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
        local_video = os.environ.get('LOCAL_VIDEO', '').strip()
        if local_video:
            # run_local --video: a copy of the footage already on this PC (no download)
            video_path = Path(local_video)
            print(f"      Local footage: {video_path}")
        elif profile['source'] == 'release':
            download_from_release(profile['release_tag'], f"{video_file_id}.mp4", str(video_path))
        else:
            download_video(video_file_id, str(video_path), service_account_info)
        print(f"      {video_path.stat().st_size / 1e6:.1f} MB")

        # Long campaign episodes get clipped many times — transcribe each one once.
        # Talk campaigns use an English-only model when the runtime allows it (whisper_models), and
        # every campaign primes Whisper with its names and terms (BACKLOG P0-4: "LOSSA VERSION").
        models = whisper_models(profile, whisper_model)
        initial_prompt, hot = whisper_hints(profile)
        cache_names, hw_tag = _transcript_cache_names(episode_name, models, hot)
        cached = None
        for cache_name in cache_names:
            try:
                cached = load_transcript_cache(cache_name, SHORTS_FOLDER_ID)
            except Exception as e:
                print(f"      Transcript cache unavailable: {e}")
                break
            if cached:
                break
        if cached:
            segments, all_words = cached['segments'], cached['words']
            print(f"[2/5] Using cached transcript {cache_name} ({len(segments)} segments, {len(all_words)} words)")
        else:
            print(f"[2/5] Transcribing with whisper-{' -> '.join(models)}"
                  f"{' (hotwords: ' + hot + ')' if hot else ''}...")
            info = {}
            segments, all_words = transcribe_video(str(video_path), model_size=models[0],
                                                   initial_prompt=initial_prompt, hotwords=hot,
                                                   fallback_models=models[1:], info=info)
            used = info.get('model', models[0])
            print(f"      {len(segments)} segments, {len(all_words)} words transcribed (whisper-{used})")
            try:
                if NO_UPLOAD:
                    raise RuntimeError('no-upload run writes nothing to Drive')
                save_transcript_cache(f"{episode_name}_{used}{hw_tag}.json", SHORTS_FOLDER_ID,
                                      segments, all_words)
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
            if NO_UPLOAD:
                print(f"\nSKIPPED (no-upload run, nothing logged): {e}")
                return
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

        if NO_UPLOAD:
            _save_no_upload(short_path, story, yt_meta, campaign)
            return

        # Never re-upload: repeat uploads get treated as reused content, seed worse and burn a slot.
        file_sha1 = _sha1(short_path)
        if yt_meta:
            dup = tracker.find_prior_upload(yt_meta['title'], file_sha1)
            if dup:
                raise RuntimeError(f"Refusing to upload: {dup}")

        # Re-check the slot now that the render is done (BACKLOG P0-8b): on 2026-10-25 a late GitHub
        # run filled MilkXP 18:45 while the local fallback rendered, and the fallback published a
        # second Short 11 min later. Nothing is uploaded or logged here, so the moment stays unused.
        publish_at = publish_note = None
        if yt_meta:
            bookings = _bookings(tracker)
            if slot and not video_file_id:
                filled = slot_filled(campaign, profile, nominal, bookings, pinned=bool(pinned_slot))
                if filled:
                    print(f"{campaign}: {filled.get('video_id') or 'a Short'} filled the {nominal} slot "
                          f"while this run rendered — not uploading.")
                    return
            # Never "now": a passed or too-close slot rolls to the next free one, and no two of our
            # publishes (any campaign) land within MIN_GAP_MIN of each other (P0-6, P0-8c).
            publish_at, publish_note = choose_publish_time(profile, bookings=bookings, nominal_iso=nominal,
                                                           campaign=campaign)
            if publish_at is None:
                print(f"{campaign}: {publish_note} — not uploading.")
                return
            if publish_note != 'slot':
                print(f"      publishAt {publish_at}: {publish_note}")

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
            if not comments_allowed(profile):
                # Brief forbids commenting (milkxp): nothing is stored, so nothing can ever be posted.
                first_comment = ''
                yt_meta['pinned_comment'] = ''
            # Row must exist before update_yt_url fills in scheduled_at / yt_url.
            tracker.log_posting_metadata(episode_name, current_run, story, yt_meta)
            try:
                tracker.log_story_fields(episode_name, current_run, story['storyline_title'],
                                         dict(_story_fields(story, first_comment), **_qa_fields(qa)))
            except Exception as e:
                print(f"::warning title=Tracker fields not written::{episode_name} run {current_run}: "
                      f"header/variant/QA columns skipped ({e})")
            from youtube_client import upload_scheduled, add_to_playlist
            # publish_at was picked right after the render (choose_publish_time): the slot, rolled
            # forward if needed; profiles without slots go out ASAP inside the US-evening window,
            # always at least MIN_GAP_MIN from our other Shorts.
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
