"""Per-IP request logging (JSONL) and a small in-memory rate limiter."""

import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "logs" / "requests.jsonl"

MAX_PER_MINUTE = 20
_hits: dict[str, deque] = defaultdict(deque)


def client_ip(request) -> str:
    """Best-guess caller IP: X-Forwarded-For if a proxy set it, else the socket peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def allow(ip: str) -> bool:
    """Sliding one-minute window per IP; False means slow down."""
    now = time.monotonic()
    hits = _hits[ip]
    while hits and now - hits[0] > 60:
        hits.popleft()
    if len(hits) >= MAX_PER_MINUTE:
        return False
    hits.append(now)
    return True


def log_request(ip: str, question: str, outcome: str, ms: int) -> None:
    """Append one JSONL line; a logging failure must never break an answer."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ip": ip,
            "question": question[:300],
            "outcome": outcome,
            "ms": ms,
        }
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log.warning("request log write failed (%s)", e)
