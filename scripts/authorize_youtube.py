from __future__ import annotations

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.config import setting_path
from core.social_uploader import YOUTUBE_SCOPE


def main() -> None:
    client_file = setting_path("YOUTUBE_CLIENT_SECRETS_FILE", "client_secret.json")
    token_file = setting_path("YOUTUBE_TOKEN_FILE", "yt_token.json")
    if not client_file.is_file():
        raise FileNotFoundError(f"YouTube OAuth client file not found: {client_file}")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_file), scopes=[YOUTUBE_SCOPE]
    )
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    print(f"Saved YouTube OAuth token to {token_file}")


if __name__ == "__main__":
    main()
