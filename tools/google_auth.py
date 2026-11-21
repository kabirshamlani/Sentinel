"""Google OAuth2 authentication for Workspace APIs (Gmail, Calendar, Drive, Docs, Chat)."""

import os
import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config.paths import SENTINEL_HOME

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
]


def get_credentials(
    client_secret_path: str | None = None,
    token_path: str | None = None,
) -> Credentials:
    client_secret_path = client_secret_path or os.getenv(
        "GOOGLE_CLIENT_SECRET_PATH",
        str(Path.cwd() / "client_secret.json"),
    )
    token_path = token_path or os.getenv(
        "GOOGLE_TOKEN_PATH",
        str(SENTINEL_HOME / "google_creds" / "token.json"),
    )

    creds = None
    token_file = Path(token_path)

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)

        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json())

    return creds


def get_docs_service(creds: Credentials | None = None):
    creds = creds or get_credentials()
    return build("docs", "v1", credentials=creds)


def get_drive_service(creds: Credentials | None = None):
    creds = creds or get_credentials()
    return build("drive", "v3", credentials=creds)


def get_gmail_service(creds: Credentials | None = None):
    creds = creds or get_credentials()
    return build("gmail", "v1", credentials=creds)


def get_calendar_service(creds: Credentials | None = None):
    creds = creds or get_credentials()
    return build("calendar", "v3", credentials=creds)


def get_chat_service(creds: Credentials | None = None):
    creds = creds or get_credentials()
    return build("chat", "v1", credentials=creds)
