import datetime
import random
import re
from googleapiclient.discovery import build
from google.oauth2 import service_account

_SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
_HEADERS = ['episode_name', 'run_number', 'clip_title',
            'start_seconds', 'end_seconds', 'score', 'reason', 'created_at']
_RANGE = 'A:H'

_POSTING_SHEET    = 'PostingTracker'
_POSTING_HEADERS  = [
    'episode_name', 'run_number', 'storyline_title', 'score',
    'yt_title', 'description', 'tags', 'pinned_comment',
    'status', 'scheduled_at', 'yt_url', 'created_at',
]
# Publish shortly after the run finishes rather than waiting for a fixed
# daily slot — the workflow's own cron trigger time is what controls which
# audience-timezone window the video lands in.
_PUBLISH_BUFFER_MIN = (10, 15)
# Column N of PostingTracker (M is whop_queue's whop_status): sha1 of the uploaded file.
_HASH_COL = 'N'
# Clip-log titles of a run the score gate skipped (its segments count as used).
SKIP_PREFIX = 'SKIPPED_'


def episode_key(name):
    """
    Dedupe key for an episode: 'Friends.S2E9.720p' and 'friends_s02e09' are
    the same episode, so both become 'friends|s02e09' (show letters before the
    code + zero-padded code). Names without an SxxEyy code are lower-cased with
    punctuation dropped.
    """
    m = re.search(r'[Ss](\d+)[ ._-]?[Ee](\d+)', name or '')
    if m:
        show = re.sub(r'[^a-z]', '', name[:m.start()].lower())
        return f"{show}|s{int(m.group(1)):02d}e{int(m.group(2)):02d}"
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def _norm_title(title):
    title = re.sub(r'#\w+', ' ', title or '')
    return ' '.join(re.findall(r"[a-z0-9']+", title.lower()))


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
        key = episode_key(episode_name)
        segs = []
        for r in self._rows():
            if r and episode_key(r[0]) == key and len(r) >= 5:
                try:
                    segs.append((float(r[3]), float(r[4])))
                except (ValueError, IndexError):
                    pass
        return segs

    def get_used_storylines(self, episode_name):
        """Return list of storyline titles + summaries already used for this episode."""
        self._ensure_posting_sheet()
        res = self._svc.spreadsheets().values().get(
            spreadsheetId=self._sid,
            range=f'{_POSTING_SHEET}!A:H',
        ).execute(num_retries=5)
        rows = res.get('values', [])[1:]
        key = episode_key(episode_name)
        storylines = []
        for r in rows:
            if r and episode_key(r[0]) == key and len(r) >= 3:
                title = r[2]  # storyline_title
                desc  = r[5] if len(r) > 5 else ''
                storylines.append(f"{title}: {desc[:120]}" if desc else title)
        return storylines

    def _ensure_posting_sheet(self):
        meta   = self._svc.spreadsheets().get(spreadsheetId=self._sid).execute(num_retries=5)
        titles = [s['properties']['title'] for s in meta['sheets']]
        if _POSTING_SHEET not in titles:
            self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self._sid,
                body={'requests': [{'addSheet': {'properties': {'title': _POSTING_SHEET}}}]},
            ).execute(num_retries=5)
            self._svc.spreadsheets().values().update(
                spreadsheetId=self._sid,
                range=f'{_POSTING_SHEET}!A1',
                valueInputOption='RAW',
                body={'values': [_POSTING_HEADERS]},
            ).execute(num_retries=5)

    def log_posting_metadata(self, episode_name, run_number, story, yt_meta):
        self._ensure_posting_sheet()
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        row = [
            episode_name,
            str(run_number),
            story['storyline_title'],
            str(story['score']),
            yt_meta.get('title', ''),
            yt_meta.get('description', ''),
            yt_meta.get('tags', ''),
            yt_meta.get('pinned_comment', ''),
            'Pending',
            '',   # upload_date — fill in manually after posting
            '',   # yt_url     — fill in manually after posting
            now,
        ]
        self._svc.spreadsheets().values().append(
            spreadsheetId=self._sid,
            range=f'{_POSTING_SHEET}!A:L',
            valueInputOption='RAW',
            body={'values': [row]},
        ).execute(num_retries=5)

    def slot_taken(self, episodes, publish_at, window_min=45, not_before=None):
        """
        True if a Short for one of these episodes is already scheduled within
        window_min of publish_at. Shorts scheduled before not_before (an earlier
        slot of the same day, when a run is pinned to one slot) don't count.
        """
        target = datetime.datetime.strptime(publish_at, '%Y-%m-%dT%H:%M:%SZ')
        floor = datetime.datetime.strptime(not_before, '%Y-%m-%dT%H:%M:%SZ') if not_before else None
        rows = self._svc.spreadsheets().values().get(
            spreadsheetId=self._sid, range=f'{_POSTING_SHEET}!A:J',
        ).execute(num_retries=5).get('values', [])
        for row in rows[1:]:
            if len(row) >= 10 and row[0] in episodes and row[9]:
                try:
                    when = datetime.datetime.strptime(row[9], '%Y-%m-%dT%H:%M:%SZ')
                except ValueError:
                    continue
                if floor and when < floor:
                    continue
                if abs((when - target).total_seconds()) <= window_min * 60:
                    return True
        return False

    def _posting_rows(self):
        self._ensure_posting_sheet()
        return self._svc.spreadsheets().values().get(
            spreadsheetId=self._sid, range=f'{_POSTING_SHEET}!A:N',
        ).execute(num_retries=5).get('values', [])[1:]

    def get_prior_titles(self):
        """(episode_name, title) for every YouTube title logged so far, oldest first."""
        return [(r[0], r[4]) for r in self._posting_rows() if len(r) > 4 and r[4]]

    def consecutive_skips(self, belongs):
        """
        How many of the latest runs (of episodes where belongs(episode) is true)
        in a row were score-gate skips, i.e. every clip-log row of the run is
        titled SKIPPED_*.
        """
        runs = {}   # (episode, run) in the order the runs were logged
        for r in self._rows():
            if len(r) >= 3 and belongs(r[0]):
                key = (r[0], r[1])
                runs[key] = runs.get(key, True) and r[2].startswith(SKIP_PREFIX)
        count = 0
        for skipped in reversed(list(runs.values())):
            if not skipped:
                break
            count += 1
        return count

    def find_prior_upload(self, title, file_sha1=None):
        """
        Why this Short must not be uploaded (an already-uploaded title or file),
        or None. Only rows that reached YouTube count, so a run that died before
        its upload does not block the retry.
        """
        norm = _norm_title(title)
        for r in self._posting_rows():
            if len(r) < 11 or not r[10]:
                continue
            if norm and _norm_title(r[4]) == norm:
                return f"title already uploaded as {r[10]} ({r[0]} run {r[1]})"
            if file_sha1 and len(r) > 13 and r[13] == file_sha1:
                return f"same video file already uploaded as {r[10]} ({r[0]} run {r[1]})"
        return None

    def get_next_publish_slot(self):
        """Return a publish time 10-15 minutes from now (just enough buffer for YouTube to process the upload)."""
        now = datetime.datetime.utcnow()
        buffer_min = random.randint(*_PUBLISH_BUFFER_MIN)
        slot = now + datetime.timedelta(minutes=buffer_min)
        return slot.strftime('%Y-%m-%dT%H:%M:%SZ')

    def update_yt_url(self, episode_name, run_number, storyline_title, yt_video_id, scheduled_at,
                      file_sha1=None):
        """Write YouTube video ID, scheduled_at and the file hash back into the PostingTracker row."""
        self._ensure_posting_sheet()
        res = self._svc.spreadsheets().values().get(
            spreadsheetId=self._sid,
            range=f'{_POSTING_SHEET}!A:N',   # through N so the file_sha1 header is seen
        ).execute(num_retries=5)
        rows = res.get('values', [])
        for i, row in enumerate(rows):
            if (len(row) >= 3 and
                    row[0] == episode_name and
                    row[1] == str(run_number) and
                    row[2] == storyline_title):
                row_num = i + 1
                self._svc.spreadsheets().values().update(
                    spreadsheetId=self._sid,
                    range=f'{_POSTING_SHEET}!I{row_num}:K{row_num}',
                    valueInputOption='RAW',
                    body={'values': [['Scheduled', scheduled_at, f'https://youtu.be/{yt_video_id}']]},
                ).execute(num_retries=5)
                if file_sha1:
                    if len(rows[0]) < 14 or rows[0][13] != 'file_sha1':
                        self._svc.spreadsheets().values().update(
                            spreadsheetId=self._sid, range=f'{_POSTING_SHEET}!{_HASH_COL}1',
                            valueInputOption='RAW', body={'values': [['file_sha1']]},
                        ).execute(num_retries=5)
                    self._svc.spreadsheets().values().update(
                        spreadsheetId=self._sid, range=f'{_POSTING_SHEET}!{_HASH_COL}{row_num}',
                        valueInputOption='RAW', body={'values': [[file_sha1]]},
                    ).execute(num_retries=5)
                return

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
