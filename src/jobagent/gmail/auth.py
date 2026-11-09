"""Gmail OAuth: get valid credentials, logging in through the browser only when needed."""

from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

# Least privilege: read-only. Changing this invalidates the saved token.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_credentials(credentials_path: Path, token_path: Path) -> Credentials:
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Expected roughly weekly: apps in "Testing" mode get 7-day refresh tokens.
            creds = None
    else:
        creds = None

    if creds is None:
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"OAuth client file not found at {credentials_path}. "
                "Download it from Google Cloud (Desktop app client) - see README."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)  # opens your browser once

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)  # it's a secret: readable by you only
    return creds


def build_service(creds: Credentials) -> Resource:
    # cache_discovery=False avoids a noisy warning about the file cache.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
