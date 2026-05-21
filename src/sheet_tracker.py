import datetime
from googleapiclient.discovery import build
from google.oauth2 import service_account

_SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
_HEADERS = ['episode_name', 'run_number', 'clip_title',
            'start_seconds', 'end_seconds', 'score', 'reason', 'created_at']
_RANGE = 'A:H'


class SheetTracker:
    def __init__(self, credentials_info, spreadsheet_id):
        creds = service_account.Credentials.from_service_account_info(
            credentials_info, scopes=_SCOPES
        )
        self._svc = build('sheets', 'v4', credentials=creds)
        self._sid = spreadsheet_id
        self._ensure_headers()

    def _ensure_headers(self):
        res = self._svc.spreadsheets().values().get(
            spreadsheetId=self._sid, range='A1:H1'
        ).execute(num_retries=5)
        if not res.get('values'):
            self._svc.spreadsheets().values().update(
                spreadsheetId=self._sid,
                range='A1',
                valueInputOption='RAW',
                body={'values': [_HEADERS]}
            ).execute(num_retries=5)

    def _rows(self):
        res = self._svc.spreadsheets().values().get(
            spreadsheetId=self._sid, range=_RANGE
        ).execute(num_retries=5)
        rows = res.get('values', [])
        return rows[1:] if len(rows) > 1 else []

    def get_run_count(self, episode_name):
        runs = {r[1] for r in self._rows() if r and r[0] == episode_name}
        return len(runs)

    def get_used_segments(self, episode_name):
        segs = []
        for r in self._rows():
            if r and r[0] == episode_name and len(r) >= 5:
                try:
                    segs.append((float(r[3]), float(r[4])))
                except (ValueError, IndexError):
                    pass
        return segs

    def log_clips(self, episode_name, run_number, clips):
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        rows = [
            [episode_name, str(run_number), c['title'],
             str(c['start_seconds']), str(c['end_seconds']),
             str(c['score']), c['reason'], now]
            for c in clips
        ]
        self._svc.spreadsheets().values().append(
            spreadsheetId=self._sid,
            range=_RANGE,
            valueInputOption='RAW',
            body={'values': rows}
        ).execute(num_retries=5)
