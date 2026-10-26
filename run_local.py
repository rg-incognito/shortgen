"""
Run the shorts pipeline on this PC instead of GitHub Actions.

    python run_local.py <campaign>                 # e.g. loveisblind, markmanson, unpacked
    python run_local.py <campaign> --if-gh-missed HH:MM
        # fallback mode (Task Scheduler, ~2 h before the slot): HH:MM is the GitHub cron's UTC
        # time ('42 16 * * *' -> 16:42). If GitHub's run for that cron is still going, wait for
        # it; then run the pipeline, which exits at once if the slot already holds a Short and
        # otherwise renders and uploads only if the slot is still empty after the render.
        # A GitHub run that "succeeded" is not trusted on its own: on 2026-10-25 the 23:15 run
        # succeeded but rolled its Short to the next day.
    python run_local.py <campaign> --slot HH:MM ...
        # pin the run to one of the campaign's publish_slots_et (PUBLISH_SLOT), for
        # campaigns with two daily slots

Secrets come from .env (gitignored), the same values as the GitHub secrets:
    GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GEMINI_API_KEY, HF_TOKEN
The service-account key, yt_token.json and the gh CLI login already live on this PC.
Logs go to logs/<utc time>_<campaign>.log; every fallback decision is appended to logs/fallback.log.
"""
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
SA_FILE = ROOT / 'parental-app-635e3-f7946c9be549.json'
LOCK = ROOT / 'logs' / 'run_local.lock'
LOCK_WAIT_MIN = 90   # fallbacks fire close together: queue behind a running one, don't skip
GH_WAIT_MIN = 40     # a GitHub run for the same cron that is still going: let it finish first
DECISIONS = ROOT / 'logs' / 'fallback.log'

# Same non-secret settings as .github/workflows/generate_shorts.yml
DEFAULTS = {
    'SPREADSHEET_ID':    '1d-GID8JCIrw89ygIKXfsyT9Fe0qtxxmsUaKORuDliOM',
    'VIDEOS_FOLDER_ID':  '1bIFaNpfBy1Bp9R14T22EV-hMsYYrLeTS',
    'SHORTS_FOLDER_ID':  '1kzFXWZQu8QQVtZKeAVvSgHcLaRFfK6tg',
    'ARCHIVE_FOLDER_ID': '1nHZCaWO_KvkV52TekkF8ecC5JtIlXqJj',
    'GITHUB_REPOSITORY': 'rg-incognito/shortgen',
    'WHISPER_MODEL':     'medium',
}


def _load_env():
    env = dict(os.environ)
    env.update(DEFAULTS)
    for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    env['GOOGLE_SERVICE_ACCOUNT_JSON'] = SA_FILE.read_text(encoding='utf-8')
    env['PYTHONIOENCODING'] = 'utf-8'
    missing = [k for k in ('GOOGLE_REFRESH_TOKEN', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GEMINI_API_KEY')
               if not env.get(k)]
    if missing:
        sys.exit(f'.env is missing: {", ".join(missing)}')
    return env


def _note(msg):
    """Print and append to logs/fallback.log (Task Scheduler drops stdout: on 2026-10-25 the
    LADbible 23:15 fallback exited without a trace)."""
    line = f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {' '.join(sys.argv[1:])}: {msg}"
    print(line)
    try:
        DECISIONS.parent.mkdir(exist_ok=True)
        with open(DECISIONS, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def cron_title(cron_hhmm):
    """'16:42' -> '[42 16 * * *]', the run title tag the workflow's run-name gives that cron."""
    h, m = map(int, cron_hhmm.split(':'))
    return f'[{m} {h} * * *]'


def _last_cron(cron_hhmm, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    h, m = map(int, cron_hhmm.split(':'))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t - datetime.timedelta(days=1) if t > now else t


def gh_run_state(cron_hhmm, runs, now=None):
    """
    'running' | 'success' | 'failed' | 'none' for the scheduled run of this cron since its last
    firing. Only runs whose title carries this cron count (run-name in generate_shorts.yml): the
    old check took any scheduled run of the shared workflow, so the 22:30 LADbible run (started
    01:24 UTC) hid the missing 23:15 one on 2026-10-25.
    """
    since = _last_cron(cron_hhmm, now)
    tag = cron_title(cron_hhmm)
    state = 'none'
    for r in runs:
        created = datetime.datetime.fromisoformat(r['createdAt'].replace('Z', '+00:00'))
        if created < since or tag not in (r.get('displayTitle') or ''):
            continue
        if r['status'] != 'completed':
            return 'running'
        if r.get('conclusion') == 'success':
            state = 'success'
        elif state != 'success':
            state = 'failed'
    return state


def _gh_runs():
    out = subprocess.run(
        ['gh', 'run', 'list', '--workflow', 'Generate Shorts', '--event', 'schedule', '--limit', '20',
         '--json', 'createdAt,status,conclusion,displayTitle', '--repo', DEFAULTS['GITHUB_REPOSITORY']],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout or '[]')


def _gh_covered(cron_hhmm):
    """True if GitHub's run for this cron succeeded or is still going (informational: the
    pipeline's own slot check decides whether anything is uploaded)."""
    try:
        return gh_run_state(cron_hhmm, _gh_runs()) in ('success', 'running')
    except Exception:
        return False


def _gh_wait(cron_hhmm):
    """Wait (up to GH_WAIT_MIN) while GitHub's run for this cron is going; return its final state."""
    deadline = time.time() + GH_WAIT_MIN * 60
    while True:
        try:
            state = gh_run_state(cron_hhmm, _gh_runs())
        except Exception as e:
            return f'unknown (gh run list failed: {e})'
        if state != 'running' or time.time() > deadline:
            return state
        time.sleep(60)


def _pid_alive(pid):
    """A lock left by a run killed in a reboot must not block later runs."""
    if not pid.isdigit():
        return False
    out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True, text=True)
    return pid in out.stdout


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    campaign = sys.argv[1]
    (ROOT / 'logs').mkdir(exist_ok=True)
    if '--if-gh-missed' in sys.argv:
        cron = sys.argv[sys.argv.index('--if-gh-missed') + 1]
        state = _gh_wait(cron)
        # Always go on: main.py exits at once if the slot already holds a Short, and re-checks
        # right before uploading, so a GitHub run that rolled its Short to tomorrow is covered too.
        _note(f'GitHub run for cron {cron_title(cron)}: {state} -> running the pipeline (it checks the slot)')
    # This PC is slow; never run two pipelines at once. A slot whose run already went out
    # makes main.py exit at once, so waiting behind another run costs nothing.
    deadline = time.time() + LOCK_WAIT_MIN * 60
    while LOCK.exists() and _pid_alive(LOCK.read_text().strip()):
        if time.time() > deadline:
            _note(f'gave up: another local run is still in progress after {LOCK_WAIT_MIN} min')
            sys.exit(f'another local run is still in progress after {LOCK_WAIT_MIN} min ({LOCK})')
        time.sleep(30)
    LOCK.write_text(str(os.getpid()))

    env = _load_env()
    env['CAMPAIGN'] = campaign
    if '--slot' in sys.argv:
        env['PUBLISH_SLOT'] = sys.argv[sys.argv.index('--slot') + 1]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M')
    log = ROOT / 'logs' / f'{stamp}_{campaign}.log'
    print(f'Running {campaign} locally — log: {log}')
    try:
        with open(log, 'w', encoding='utf-8') as f:
            rc = subprocess.run([sys.executable, '-u', str(ROOT / 'src' / 'main.py')],
                                cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT).returncode
    finally:
        LOCK.unlink(missing_ok=True)
    _note(f'exit {rc} (log {log.name})')
    sys.exit(rc)


if __name__ == '__main__':
    main()
