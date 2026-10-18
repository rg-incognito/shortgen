"""
Run the shorts pipeline on this PC instead of GitHub Actions.

    python run_local.py <campaign>                 # e.g. loveisblind, markmanson, unpacked
    python run_local.py <campaign> --if-gh-missed HH:MM
        # fallback mode: only run if GitHub Actions has no successful / running
        # scheduled run since today's HH:MM UTC slot (Actions minutes ran out, etc.)

Secrets come from .env (gitignored), the same values as the GitHub secrets:
    GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GEMINI_API_KEY, HF_TOKEN
The service-account key, yt_token.json and the gh CLI login already live on this PC.
Logs go to logs/<utc time>_<campaign>.log.
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SA_FILE = ROOT / 'parental-app-635e3-f7946c9be549.json'
LOCK = ROOT / 'logs' / 'run_local.lock'

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


def _gh_covered(slot_hhmm):
    """True if GitHub Actions has a scheduled run since today's slot that succeeded or is still going."""
    now = datetime.datetime.now(datetime.timezone.utc)
    h, m = map(int, slot_hhmm.split(':'))
    slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if slot > now:
        slot -= datetime.timedelta(days=1)
    out = subprocess.run(
        ['gh', 'run', 'list', '--workflow', 'Generate Shorts', '--event', 'schedule', '--limit', '10',
         '--json', 'createdAt,status,conclusion', '--repo', DEFAULTS['GITHUB_REPOSITORY']],
        capture_output=True, text=True)
    if out.returncode != 0:
        print(f'gh run list failed ({out.stderr.strip()}) — running locally to be safe')
        return False
    for r in json.loads(out.stdout or '[]'):
        created = datetime.datetime.fromisoformat(r['createdAt'].replace('Z', '+00:00'))
        if created >= slot and (r['status'] != 'completed' or r['conclusion'] == 'success'):
            return True
    return False


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
    if '--if-gh-missed' in sys.argv:
        slot = sys.argv[sys.argv.index('--if-gh-missed') + 1]
        if _gh_covered(slot):
            print(f'GitHub Actions covered the {slot} UTC slot — nothing to do')
            return

    (ROOT / 'logs').mkdir(exist_ok=True)
    # This PC is slow; never run two pipelines at once.
    if LOCK.exists() and _pid_alive(LOCK.read_text().strip()):
        sys.exit(f'another local run is in progress ({LOCK})')
    LOCK.write_text(str(os.getpid()))

    env = _load_env()
    env['CAMPAIGN'] = campaign
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M')
    log = ROOT / 'logs' / f'{stamp}_{campaign}.log'
    print(f'Running {campaign} locally — log: {log}')
    try:
        with open(log, 'w', encoding='utf-8') as f:
            rc = subprocess.run([sys.executable, '-u', str(ROOT / 'src' / 'main.py')],
                                cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT).returncode
    finally:
        LOCK.unlink(missing_ok=True)
    print(f'exit {rc}')
    sys.exit(rc)


if __name__ == '__main__':
    main()
