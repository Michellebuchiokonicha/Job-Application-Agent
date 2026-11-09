"""Settings, read once from environment variables (and .env, if present)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Broad on purpose: rejections rarely contain "application" or "interview", so the
# classifier (not the search query) decides what's relevant. Promotions and social
# tabs are excluded because they are never recruiter mail.
DEFAULT_GMAIL_QUERY = "-in:chats -category:promotions -category:social"


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
                os.getenv("GMAIL_CREDENTIALS_PATH", "secrets/credentials.json")
            ),
            gmail_token_path=Path(os.getenv("GMAIL_TOKEN_PATH", "secrets/token.json")),
            gmail_query=os.getenv("GMAIL_QUERY") or DEFAULT_GMAIL_QUERY,
            db_path=Path(os.getenv("DB_PATH", "data/jobagent.db")),
        )
