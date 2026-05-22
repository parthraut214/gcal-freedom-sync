#!/usr/bin/env python3
"""
Run this ONCE on any machine with a browser to authorize Google Calendar access.
It will produce token.json — copy that file to the Pi.

Usage:
  python auth_google.py                     # opens browser on this machine
  python auth_google.py --headless          # prints URL + waits for code paste (Pi-friendly)
"""

import sys
import os
import yaml
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']


def load_config():
    with open('config.yaml') as f:
        return yaml.safe_load(f)


def main():
    config = load_config()
    creds_path = config['credentials']['google_credentials_path']
    token_path = config['credentials']['google_token_path']
    headless = '--headless' in sys.argv

    # Refresh if we already have a token
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if creds.valid:
            print(f"Token at {token_path!r} is already valid. Nothing to do.")
            return
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, 'w') as f:
                f.write(creds.to_json())
            print(f"Token refreshed and saved to {token_path!r}.")
            return

    if not os.path.exists(creds_path):
        print(f"ERROR: {creds_path!r} not found.")
        print("Download OAuth 2.0 credentials from Google Cloud Console:")
        print("  APIs & Services → Credentials → Create → OAuth client ID → Desktop app")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)

    if headless:
        print("Opening headless auth flow — visit the URL below in any browser:")
        creds = flow.run_console()
    else:
        creds = flow.run_local_server(port=0)

    with open(token_path, 'w') as f:
        f.write(creds.to_json())

    print(f"\nAuthorization complete. Token saved to {token_path!r}.")
    if not headless:
        print(f"Copy this file to vpn-node if you ran auth on a different machine:")
        print(f"  scp {token_path} pi@192.168.1.115:/opt/gcal-freedom-sync/")


if __name__ == '__main__':
    main()
