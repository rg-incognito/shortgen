"""
One-time: create .env for run_local.py (local runs of the pipeline).

Copies HF_TOKEN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GEMINI_API_KEY from
tokens.txt and gets a fresh Drive refresh token through the browser sign-in
(same scopes as setup_oauth.py). Nothing secret is printed.

    python setup_local_env.py
"""
import re
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).parent
CLIENT_FILE = ROOT / 'client_secret_1061831573768-v5195flb6ql1a38fva5ic0a5b0gm80iq.apps.googleusercontent.com.json'
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.readonly',
]

values = {}
for line in (ROOT / 'tokens.txt').read_text(encoding='utf-8').splitlines():
    m = re.match(r'#?\s*(HF_TOKEN|GOOGLE_CLIENT_ID|GOOGLE_CLIENT_SECRET|GEMINI_API_KEY)\s*(?:=|→)\s*(\S+)', line)
    if m:
        values[m.group(1)] = m.group(2)

creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES).run_local_server(port=0)
values['GOOGLE_REFRESH_TOKEN'] = creds.refresh_token

(ROOT / '.env').write_text(
    '# Secrets for run_local.py — gitignored, never commit\n'
    + ''.join(f'{k}={v}\n' for k, v in values.items()),
    encoding='utf-8')
print('Wrote .env with:', ', '.join(sorted(values)))
