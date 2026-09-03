"""Per-link rate guard - makes MISO's one-request-per-minute rule mechanical.

MISO's published limit is one request per link per minute, and they IP-ban
scrapers. This module is the only thing that makes that limit true no matter
how the poller was started: a fresh `uvicorn --reload` on every file save, a
crash-restart loop, a stray `--once` alongside the scheduler, or a cycle that
overran a minute because MISO was slow.

The mechanism is a lease claimed BEFORE each request, not after:

    with claim("https://public-api.misoenergy.org/api/FuelMix") as allowed:
        if allowed:
            requests.get(...)

State lives at ~/.cache/miso-copilot/rate-guard.json, deliberately outside the
repo and outside data/, so that no MISO_RAW_DIR override and no `rm -rf data/`
can disable it.

See the specification, section 4.3.
"""

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

MIN_SECONDS_BETWEEN = 60
STALE_AFTER = timedelta(hours=1)
LOCK_TIMEOUT_SECONDS = 10


def guard_path() -> Path:
    """Where the lease file lives. Honors XDG_CACHE_HOME, defaults to ~/.cache."""
    cache = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(cache) / "miso-copilot" / "rate-guard.json"


def _read(path: Path) -> dict:
    """Load the lease file. Anything unusable is treated as empty, never an error."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("rate guard: unreadable lease file (%s), treating as empty", e)
        return {}
    if not isinstance(data, dict):
        log.warning("rate guard: lease file is not an object, treating as empty")
        return {}
    return data


def _decide(stored: str | None, now: datetime) -> tuple[bool, str | None]:
    """Should this request proceed? Returns (allowed, warning to log).

    Every abnormal stored value proceeds rather than blocks. A guard that
    fails closed on a garbled timestamp would silently stop all polling, and
    a stale lease is a far smaller problem than a dead poller nobody noticed.
    """
    if stored is None:
        return True, None

    try:
        last = datetime.fromisoformat(stored)
    except (TypeError, ValueError):
        return True, f"unparseable lease timestamp {stored!r}, proceeding"

    if last.tzinfo is None:
        return True, f"naive lease timestamp {stored!r}, proceeding"

    age = now - last

    # A negative age means the clock moved backward (an NTP step, a laptop
    # waking, a lease file copied from another machine). Treating "in the
    # future" as "recent" would block every request until wall-clock time
    # caught up, which is a silent total outage.
    if age < timedelta(0):
        return True, f"lease timestamp {stored!r} is in the future, proceeding"

    if age < timedelta(seconds=MIN_SECONDS_BETWEEN):
        return False, None

    if age > STALE_AFTER:
        return True, f"no request to this link for {age}, poller was down"

    return True, None


@contextmanager
def claim(url: str, now: datetime):
    """Claim the right to request `url`, yielding True if allowed.

    Holds an exclusive lock across read, decide, and write, so two processes
    starting at the same moment cannot both pass on the same stale value. The
    timestamp is written before the request is made, so a process killed
    mid-fetch still leaves the claim behind.
    """
    path = guard_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Fail closed. A poller that cannot prove it is under the limit does
        # not fetch - better a stale demo than an IP ban.
        log.error("rate guard: cannot create %s (%s), refusing to fetch", path.parent, e)
        yield False
        return

    handle = None
    try:
        handle = _lock(path)
    except OSError as e:
        log.error("rate guard: cannot lock %s (%s), refusing to fetch", path, e)
        yield False
        return

    try:
        leases = _read(path)
        allowed, warning = _decide(leases.get(url), now)
        if warning:
            log.warning("rate guard: %s (%s)", warning, url)
        if allowed:
            leases[url] = now.isoformat()
            try:
                _write(path, leases)
            except OSError as e:
                log.error("rate guard: cannot write %s (%s), refusing to fetch", path, e)
                yield False
                return
        else:
            log.warning("rate guard: skipping %s, requested less than %ds ago",
                        url, MIN_SECONDS_BETWEEN)
        yield allowed
    finally:
        if handle is not None:
            _unlock(handle)


def _lock(path: Path):
    """Take an exclusive lock beside the lease file, waiting briefly if held."""
    lock_file = path.with_suffix(".lock")
    handle = open(lock_file, "w")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            _flock(handle)
            return handle
        except OSError:
            if time.monotonic() > deadline:
                handle.close()
                raise
            time.sleep(0.05)


def _flock(handle) -> None:
    """Exclusive non-blocking lock. Split out so the import stays POSIX-only."""
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    """Release the lock and close the handle, never raising."""
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass


def _write(path: Path, leases: dict) -> None:
    """Replace the lease file atomically, so a crash never truncates it.

    core is imported here rather than at module scope because core imports
    this module; a deferred import keeps the dependency one-directional.
    """
    from backend.poller import core

    core.write_atomic(path, json.dumps(leases, indent=2).encode())
