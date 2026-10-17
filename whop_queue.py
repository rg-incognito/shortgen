"""
Whop submission queue — read/write the PostingTracker sheet for campaign shorts.

    python whop_queue.py due                 # live campaign shorts not yet submitted (JSON)
    python whop_queue.py due --fresh         # ...only those still inside Whop's 30 min submit window
    python whop_queue.py mark <yt_url> <status>   # e.g. submitted / approved / rejected: reason

Run locally (uses the service-account key in this folder). The Whop side —
opening the campaign and pasting the link — is done in Chrome by Claude, with
the final submit click approved by the user.
"""
import datetime
import json
import sys
from pathlib import Path

from googleapiclient.discovery import build
from google.oauth2 import service_account

sys.path.insert(0, str(Path(__file__).parent / 'src'))
from campaigns import CAMPAIGNS

SA_FILE        = Path(__file__).parent / 'parental-app-635e3-f7946c9be549.json'
SPREADSHEET_ID = '1d-GID8JCIrw89ygIKXfsyT9Fe0qtxxmsUaKORuDliOM'
SHEET          = 'PostingTracker'
WHOP_COL       = 'M'   # column after created_at (L)
# Whop's submit form rejects clips not "posted within the last 30 minutes".
SUBMIT_WINDOW_MIN = 30

# Episode name → campaign profile, for rows written by campaign runs.
_EPISODES = {
    ep: name
    for name, prof in CAMPAIGNS.items() if prof.get('whop_campaign')
    for _, ep in prof.get('files', [])
}


def _svc():
    creds = service_account.Credentials.from_service_account_file(
        str(SA_FILE), scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return build('sheets', 'v4', credentials=creds)


def _rows(svc):
    res = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f'{SHEET}!A:M').execute(num_retries=5)
    return res.get('values', [])


def _campaign_for(episode):
    return _EPISODES.get(episode)


def due(fresh=False):
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    out = []
    for i, r in enumerate(_rows(_svc())[1:], start=2):
        r = r + [''] * (13 - len(r))
        episode, title, scheduled_at, yt_url, whop = r[0], r[4], r[9], r[10], r[12]
        campaign = _campaign_for(episode)
        if not campaign or not yt_url or whop:
            continue
        try:
            age_min = (now - datetime.datetime.strptime(scheduled_at, '%Y-%m-%dT%H:%M:%SZ')).total_seconds() / 60
        except ValueError:
            continue
        if age_min >= 0 and (not fresh or age_min < SUBMIT_WINDOW_MIN):
            out.append({
                'row': i, 'campaign': campaign, 'episode': episode, 'title': title,
                'yt_url': yt_url, 'published_at': scheduled_at, 'age_min': round(age_min, 1),
                'whop_campaign': CAMPAIGNS[campaign].get('whop_campaigns', {}).get(
                    episode, CAMPAIGNS[campaign]['whop_campaign']),
            })
    print(json.dumps(out, indent=2, ensure_ascii=False))


def mark(yt_url, status):
    svc = _svc()
    rows = _rows(svc)
    if rows and (len(rows[0]) < 13 or rows[0][12] != 'whop_status'):
        svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range=f'{SHEET}!{WHOP_COL}1',
            valueInputOption='RAW', body={'values': [['whop_status']]}).execute(num_retries=5)
    stamp = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M UTC')
    for i, r in enumerate(rows[1:], start=2):
        if len(r) > 10 and r[10] == yt_url:
            svc.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID, range=f'{SHEET}!{WHOP_COL}{i}',
                valueInputOption='RAW', body={'values': [[f'{status} ({stamp})']]}).execute(num_retries=5)
            print(f'row {i}: {status}')
            return
    sys.exit(f'No PostingTracker row with yt_url {yt_url}')


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == 'due':
        due(fresh='--fresh' in sys.argv)
    elif len(sys.argv) >= 4 and sys.argv[1] == 'mark':
        mark(sys.argv[2], ' '.join(sys.argv[3:]))
    else:
        sys.exit(__doc__)
