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
        print(f"  Uploaded {name} → drive id: {result.get('id')}")
