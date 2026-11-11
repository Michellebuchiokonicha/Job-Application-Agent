"""Settings, read once from environment variables (and .env, if present)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Broad on purpose: rejections rarely contain "application" or "interview", so the
# classifier (not the search query) decides what's relevant. Excluded: chats, your own
# sent mail, and the promotions/social tabs (never recruiter mail).
DEFAULT_GMAIL_QUERY = "-in:chats -in:sent -category:promotions -category:social"

# Outside ~/Documents on purpose: Documents is often synced to iCloud, and this
# database holds a copy of your email. Keep it local-only.
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "jobagent" / "jobagent.db"
# Same reason for secrets: the token grants read access to your Gmail.
SECRETS_DIR = Path.home() / ".config" / "jobagent"


@dataclass(frozen=True)
class Settings:
    gmail_credentials_path: Path
    gmail_token_path: Path
    gmail_query: str
    db_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()  # no-op if there is no .env file
        return cls(
            gmail_credentials_path=Path(
                os.getenv("GMAIL_CREDENTIALS_PATH") or SECRETS_DIR / "credentials.json"
            ).expanduser(),
            gmail_token_path=Path(
                os.getenv("GMAIL_TOKEN_PATH") or SECRETS_DIR / "token.json"
            ).expanduser(),
            gmail_query=os.getenv("GMAIL_QUERY") or DEFAULT_GMAIL_QUERY,
            db_path=Path(os.getenv("DB_PATH") or DEFAULT_DB_PATH).expanduser(),
        )
