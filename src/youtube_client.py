"""YouTube Data API v3 — upload a vertical short as a scheduled private post."""
import json
import os
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# youtube scope is a superset of youtube.upload and also covers playlist management.
# NOTE: if the stored token only has youtube.upload, re-run setup_yt_oauth.py locally
# and update the YT_TOKEN_JSON secret with the new token.
_SCOPES = ['https://www.googleapis.com/auth/youtube']
_TOKEN_FILE = os.path.join(os.path.dirname(__file__), '..', 'yt_token.json')


def _build_svc():
    """Load credentials from yt_token.json (written from YT_TOKEN_JSON secret at runtime)."""
    if not os.path.exists(_TOKEN_FILE):
        raise FileNotFoundError("yt_token.json not found — set YT_TOKEN_JSON secret")
    raw = open(_TOKEN_FILE, encoding='utf-8-sig').read()
    info = json.loads(raw)
    creds = Credentials.from_authorized_user_info(info, _SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(_TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return build('youtube', 'v3', credentials=creds)


def _get_or_create_playlist(svc, title):
    """Return playlist ID for the named playlist on the channel, creating it if absent."""
    resp = svc.playlists().list(part='snippet', mine=True, maxResults=50).execute()
    for item in resp.get('items', []):
        if item['snippet']['title'] == title:
            return item['id']
    created = svc.playlists().insert(
        part='snippet,status',
        body={
            'snippet': {'title': title, 'defaultLanguage': 'en'},
            'status':  {'privacyStatus': 'public'},
        }
    ).execute()
    return created['id']


def add_to_playlist(video_id, playlist_title):
    """Add video_id to the named playlist, creating the playlist if it doesn't exist."""
    svc = _build_svc()
    playlist_id = _get_or_create_playlist(svc, playlist_title)
    svc.playlistItems().insert(
        part='snippet',
        body={
            'snippet': {
                'playlistId': playlist_id,
                'resourceId': {'kind': 'youtube#video', 'videoId': video_id},
            }
        }
    ).execute()
    return playlist_id


def own_schedule(svc=None, max_videos=50):
    """
    Publish times of this channel's most recent uploads (read only, ~3 quota units):
    [{'video_id', 'title', 'when' (ISO), 'state': 'scheduled' | 'public'}]. A scheduled
    private video has status.publishAt; a public one counts at snippet.publishedAt.
    Private / unlisted videos with no publishAt never go out and are left out.
    """
    svc = svc or _build_svc()
    ch = svc.channels().list(part='contentDetails', mine=True).execute()
    items = ch.get('items') or []
    if not items:
        return []
    uploads = items[0]['contentDetails']['relatedPlaylists']['uploads']
    pl = svc.playlistItems().list(part='contentDetails', playlistId=uploads,
                                  maxResults=min(max_videos, 50)).execute()
    ids = [i['contentDetails']['videoId'] for i in pl.get('items', [])]
    out = []
    if not ids:
        return out
    vids = svc.videos().list(part='snippet,status', id=','.join(ids), maxResults=50).execute()
    for v in vids.get('items', []):
        status, snippet = v.get('status', {}), v.get('snippet', {})
        if status.get('publishAt') and status.get('privacyStatus') != 'public':
            out.append({'video_id': v['id'], 'title': snippet.get('title', ''),
                        'when': status['publishAt'], 'state': 'scheduled'})
        elif status.get('privacyStatus') == 'public' and snippet.get('publishedAt'):
            out.append({'video_id': v['id'], 'title': snippet.get('title', ''),
                        'when': snippet['publishedAt'], 'state': 'public'})
    return out


def upload_body(title, description, tags_str, publish_at_iso, paid_promotion=False):
    """
    (part, body) for videos.insert. paid_promotion sets the "includes paid
    promotion" disclosure (videos resource paidProductPlacementDetails.
    hasPaidProductPlacement, its own part); only campaigns whose brief requires
    the paid-partnership toggle pass it.
    """
    tags = [t.strip() for t in tags_str.split(',') if t.strip()]
    body = {
        'snippet': {
            'title':           title[:100],
            'description':     description,
            'tags':            tags,
            'categoryId':      '24',   # Entertainment
            # Explicit US-English text and audio: one of the few signals that
            # matches a Short to its viewers' language on this IN-country channel.
            'defaultLanguage':      'en-US',
            'defaultAudioLanguage': 'en-US',
        },
        'status': {
            'privacyStatus':          'private',
            'publishAt':              publish_at_iso,
            'selfDeclaredMadeForKids': False,
            'madeForKids':             False,
        },
    }
    part = 'snippet,status'
    if paid_promotion:
        body['paidProductPlacementDetails'] = {'hasPaidProductPlacement': True}
        part += ',paidProductPlacementDetails'
    return part, body


def upload_scheduled(video_path, title, description, tags_str, publish_at_iso, paid_promotion=False):
    """
    Upload a video to YouTube as a private scheduled post.

    publish_at_iso — ISO 8601 UTC string, e.g. '2026-05-23T09:00:00Z'
    paid_promotion — tick "includes paid promotion" (campaign briefs that require it).
    Returns the YouTube video ID.
    """
    # An @handle-first title reads as promo/spam and gets no curiosity hook.
    if title.lstrip().startswith('@'):
        raise ValueError(f"Refusing to upload a title that starts with an @handle: {title!r}")
    part, body = upload_body(title, description, tags_str, publish_at_iso, paid_promotion)
    svc  = _build_svc()

    media = MediaFileUpload(
        video_path, mimetype='video/mp4',
        resumable=True, chunksize=5 * 1024 * 1024,
    )
    req = svc.videos().insert(part=part, body=body, media_body=media)

    response = None
    while response is None:
        status, response = req.next_chunk()
        if status:
            print(f"    YT upload {int(status.progress() * 100)}%")

    return response['id']


# Posting the first comment is a write action the user has not approved yet, so
# it only runs when AUTO_FIRST_COMMENT=1 is set (and fetch_analytics.py
# --post-first-comments is passed). commentThreads.insert may need the
# youtube.force-ssl scope: re-run setup_yt_oauth.py with it if it returns 403.
def first_comment_enabled():
    return os.environ.get('AUTO_FIRST_COMMENT', '').strip() == '1'


def post_comment(video_id, text):
    """Post text as a top-level comment on a public video; returns the comment thread id."""
    if not first_comment_enabled():
        raise RuntimeError('first-comment posting is off (set AUTO_FIRST_COMMENT=1 to enable)')
    text = (text or '').strip()
    if not text:
        raise ValueError('empty comment')
    svc = _build_svc()
    resp = svc.commentThreads().insert(
        part='snippet',
        body={'snippet': {'videoId': video_id,
                          'topLevelComment': {'snippet': {'textOriginal': text[:500]}}}},
    ).execute()
    return resp['id']
