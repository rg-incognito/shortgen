import os
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.oauth2 import service_account

_SCOPES = ['https://www.googleapis.com/auth/drive']


def _service(credentials_info):
    creds = service_account.Credentials.from_service_account_info(
        credentials_info, scopes=_SCOPES
    )
    return build('drive', 'v3', credentials=creds)


def list_videos(folder_id, credentials_info):
    svc = _service(credentials_info)
    res = svc.files().list(
        q=f"'{folder_id}' in parents and mimeType contains 'video/' and trashed=false",
        fields='files(id, name)',
        orderBy='name'
    ).execute()
    return [(f['id'], f['name']) for f in res.get('files', [])]


def pick_video(folder_id, credentials_info, tracker):
    """
    Return (file_id, episode_name) for the next video to process.
    Prefers videos that have had 1 run (need a 2nd) over fresh ones (0 runs).
    Skips videos already at MAX_RUNS (should have been archived).
    """
    videos = list_videos(folder_id, credentials_info)
    if not videos:
        return None, None

    run1, run0 = [], []
    for file_id, name in videos:
        episode = os.path.splitext(name)[0]
        count = tracker.get_run_count(episode)
        if count == 0:
            run0.append((file_id, episode))
        elif count == 1:
            run1.append((file_id, episode))

    chosen = run1 or run0
    if not chosen:
        return None, None

    return chosen[0]


def move_to_archive(file_id, archive_folder_id, videos_folder_id, credentials_info):
    svc = _service(credentials_info)
    svc.files().update(
        fileId=file_id,
        addParents=archive_folder_id,
        removeParents=videos_folder_id,
        fields='id, parents'
    ).execute()


def download_video(file_id, output_path, credentials_info):
    svc = _service(credentials_info)
    request = svc.files().get_media(fileId=file_id)
    with open(output_path, 'wb') as f:
        dl = MediaIoBaseDownload(f, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            status, done = dl.next_chunk()
            print(f"  Download {int(status.progress() * 100)}%")


def upload_shorts(file_paths, folder_id, credentials_info):
    svc = _service(credentials_info)
    for path in file_paths:
        name = os.path.basename(path)
        metadata = {'name': name, 'parents': [folder_id]}
        media = MediaFileUpload(path, mimetype='video/mp4', resumable=True)
        result = svc.files().create(
            body=metadata, media_body=media, fields='id'
        ).execute()
        print(f"  Uploaded {name} → {result.get('id')}")
