"""Your contact details for application forms: name, email, phone, links.

They live outside the repo (they're personal), in a TOML file you write. Any key can be
left out or empty: that field then comes to you instead of being filled.
"""

import tomllib
from pathlib import Path

DEFAULT_PROFILE_PATH = Path.home() / ".local" / "share" / "jobagent" / "profile.toml"

# Every key the forms can use, with what it's for (shown when the file is missing).
PROFILE_KEYS = {
    "name": "Full name, as on your CV",
    "email": "Email for applications",
    "phone": "Phone, e.g. +44 7...",
    "location": "Current location, e.g. London, UK",
    "current_company": "Current company (or leave empty)",
    "linkedin": "LinkedIn URL",
    "github": "GitHub URL",
    "portfolio": "Portfolio or personal site URL",
}


class ProfileError(Exception):
    """The profile file is missing or broken. The message says how to fix it."""


def load_profile(path: Path | None = None) -> dict[str, str]:
    path = path or DEFAULT_PROFILE_PATH  # looked up at call time, so tests can swap it
    if not path.exists():
        example = "\n".join(f'{k} = ""  # {v}' for k, v in PROFILE_KEYS.items())
        raise ProfileError(f"No profile yet. Create {path} like this:\n{example}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"{path} isn't valid TOML: {e}") from None
    unknown = sorted(set(data) - set(PROFILE_KEYS))
    if unknown:
        raise ProfileError(f"{path} has unknown keys: {', '.join(unknown)}")
    return {k: str(v).strip() for k, v in data.items()}
