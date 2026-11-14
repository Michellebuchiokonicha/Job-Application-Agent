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

# Local model: emails never leave the machine.
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_INBOX_MODEL = "qwen3:8b"


@dataclass(frozen=True)
class Settings:
    gmail_credentials_path: Path
    gmail_token_path: Path
    gmail_query: str
    db_path: Path
    ollama_host: str = DEFAULT_OLLAMA_HOST
    inbox_model: str = DEFAULT_INBOX_MODEL
    reed_api_key: str | None = None

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
            ollama_host=os.getenv("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST,
            inbox_model=os.getenv("INBOX_MODEL") or DEFAULT_INBOX_MODEL,
            reed_api_key=os.getenv("REED_API_KEY") or None,
        )
