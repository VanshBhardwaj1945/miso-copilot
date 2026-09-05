"""Env loading and shared constants for the backend."""

import os
from pathlib import Path

MODEL = "claude-opus-5"

CONTACT_URL = "https://www.misoenergy.org/about/contact-us/"
# old "real-time-displays" page 404s now; MISO moved it here (checked 2026-09-05)
REALTIME_URL = (
    "https://www.misoenergy.org/markets-and-operations/"
    "real-time--market-data/markets-displays/"
)
MISO_HOME_URL = "https://www.misoenergy.org"

# From the Corporate Fact Sheet map (June 2026): MISO serves all or PART of
# these 15 states, plus Manitoba. Regions are approximate - MISO's real
# boundaries follow member utilities, not state lines.
MISO_STATES = {
    "North": ["MN", "ND", "SD", "MT", "WI", "IA"],
    "Central": ["IL", "IN", "MI", "MO", "KY"],
    "South": ["AR", "LA", "MS", "TX"],
}


def _load_dotenv() -> None:
    """Tiny .env loader so we don't need python-dotenv."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
