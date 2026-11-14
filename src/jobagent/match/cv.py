"""Your CVs, as plain text for the matcher. They live outside the repo, readable only by you."""

import re
from functools import cache
from pathlib import Path

from pypdf import PdfReader

DEFAULT_CV_DIR = Path.home() / ".local" / "share" / "jobagent" / "cv"
CV_FILES = {
    "ai_engineer": "cv_ai_engineer.pdf",
    "forward_deployed": "cv_forward_deployed_engineer.pdf",
}

# Client-facing roles get the CV that leads with client work.
_CLIENT_FACING = re.compile(
    r"\b(forward[- ]deployed|solutions? (architect|engineer)|customer|client|"
    r"pre-?sales|consultant)\b"
)
_EMAIL = re.compile(r"[\w.%+-]+@[\w.-]+\.[a-z]{2,}", re.IGNORECASE)


def cv_for(title: str) -> str:
    """Which CV to compare a job against: its key in CV_FILES."""
    return "forward_deployed" if _CLIENT_FACING.search(title.lower()) else "ai_engineer"


@cache  # read each PDF once per run
def load_cv(key: str, cv_dir: Path = DEFAULT_CV_DIR) -> str:
    path = cv_dir / CV_FILES[key]
    if not path.exists():
        raise FileNotFoundError(f"CV not found: {path}")
    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    # Data minimisation: the model needs skills and experience, not contact details.
    return _EMAIL.sub("[email]", text)
