"""
Campaign footage that doesn't live in Drive.

YouTube blocks downloads from CI runners, so YouTube-sourced episodes are
downloaded locally and attached to a private GitHub release; CI pulls them
back with its own GITHUB_TOKEN.
"""
import os
import subprocess

import requests

_SPONSORBLOCK = 'https://sponsor.ajay.app/api/skipSegments'


def download_from_release(tag, asset_name, output_path):
    cmd = ['gh', 'release', 'download', tag, '--pattern', asset_name,
           '--output', output_path, '--clobber']
    repo = os.environ.get('GITHUB_REPOSITORY')
    if repo:
        cmd += ['--repo', repo]
    subprocess.run(cmd, check=True)


def sponsor_segments(youtube_id):
    """Known ad-read / self-promo spans for a YouTube video, as [(start, end)]."""
    try:
        resp = requests.get(
            _SPONSORBLOCK,
            params={'videoID': youtube_id,
                    'categories': '["sponsor","selfpromo","interaction"]'},
            timeout=20,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return [tuple(s['segment']) for s in resp.json()]
    except Exception as e:
        print(f"      SponsorBlock lookup failed: {e}")
        return []
