"""Per-link rate guard - makes MISO's one-request-per-minute rule mechanical.

MISO's published limit is one request per link per minute, and they IP-ban
scrapers. This module is the only thing that makes that limit true no matter
how the poller was started: a fresh `uvicorn --reload` on every file save, a
crash-restart loop, a stray `--once` alongside the scheduler, or a cycle that
overran a minute because MISO was slow.

The mechanism is a lease claimed BEFORE each request, not after:

    if claim("https://public-api.misoenergy.org/api/FuelMix"):
        requests.get(...)

State lives at ~/.cache/miso-copilot/rate-guard.json, deliberately outside the
repo and outside data/, so no MISO_RAW_DIR override or `rm -rf data/` disables it.
"""

import contextlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

MIN_SECONDS_BETWEEN = 60
STALE_AFTER = timedelta(hours=1)
LOCK_TIMEOUT_SECONDS = 10

# How far a lease may sit in the future before we believe the clock, rather
# than our own bookkeeping, moved. Anything smaller is two processes racing on
# the same link within the same handful of milliseconds, which must be denied.
CLOCK_BACKWARD_TOLERANCE = timedelta(seconds=5)


class GuardUnreadableError(Exception):
    """The lease file exists but cannot be trusted - fatal, the guard fails closed."""


def guard_path() -> Path:
    """Where the lease file lives. Honors XDG_CACHE_HOME, defaults to ~/.cache."""
    cache = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(cache) / "miso-copilot" / "rate-guard.json"


def _now() -> datetime:
    """core.now(), imported late because core imports this module."""
    from backend.poller import core

    return core.now()


def _read(path: Path) -> dict[str, str]:
    """Load the lease file (URL -> ISO claim time). Missing = first run; unreadable = fail closed."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise GuardUnreadableError(
            f"cannot read lease file ({e.strerror})") from e
    except (ValueError, RecursionError) as e:
        raise GuardUnreadableError(f"lease file is not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise GuardUnreadableError("lease file is not a JSON object")
    return data


def _decide(stored: object, now: datetime) -> tuple[bool, str | None]:
    """Should this request proceed? Returns (allowed, warning to log).

    A garbled value proceeds with a warning - one extra request beats a dead
    poller nobody noticed. An unreadable file is _read's job, and fails closed.
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

    # well into the future = the clock moved backward; treating that as "recent"
    # would block until the clock caught up. A few ms ahead is a racing process.
    if age < -CLOCK_BACKWARD_TOLERANCE:
        return True, f"lease timestamp {stored!r} is in the future, proceeding"

    if age < timedelta(seconds=MIN_SECONDS_BETWEEN):
        return False, None

    if age > STALE_AFTER:
        return True, f"no request to this link for {age}, poller was down"

    return True, None


def claim(url: str) -> bool:
    """Claim the right to request `url`. True when the request may proceed.

    The timestamp is taken under the lock (a caller-supplied one let racing
    processes both win), the lock covers only read-decide-write (never the
    fetch), and the lease is written before the request so a process killed
    mid-fetch still leaves its claim behind.
    """
    path = guard_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # fail closed: a poller that cannot prove it is under the limit does not fetch
        log.error("rate guard: cannot create %s (%s), refusing to fetch",
                  path.parent, e)
        return False

    try:
        handle = _lock(path)
    except OSError as e:
        log.error("rate guard: cannot lock %s (%s), refusing to fetch", path, e)
        return False

    try:
        try:
            leases = _read(path)
        except GuardUnreadableError as e:
            log.error("rate guard: %s at %s, refusing to fetch", e, path)
            return False

        now = _now()
        allowed, warning = _decide(leases.get(url), now)
        if warning:
            log.warning("rate guard: %s (%s)", warning, url)
        if not allowed:
            log.warning("rate guard: skipping %s, requested less than %ds ago",
                        url, MIN_SECONDS_BETWEEN)
            return False

        leases[url] = now.isoformat()
        try:
            _write(path, leases)
        except OSError as e:
            log.error("rate guard: cannot write %s (%s), refusing to fetch",
                      path, e)
            return False
        return True
    finally:
        _unlock(handle)


@contextlib.contextmanager
def file_lock(path: Path):
    """Hold an exclusive lock beside `path` for the body of the `with`.

    Used by core for the status file (a different lock file from the leases).
    A lock that cannot be taken is logged and the body runs anyway. Never hold
    it across a network call - LOCK_TIMEOUT_SECONDS is shorter than a fetch.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = _lock(path)
    except OSError as e:
        log.warning("could not lock %s (%s), proceeding unserialized", path, e)
        yield False
        return
    try:
        yield True
    finally:
        _unlock(handle)


def _lock(path: Path):
    """Take an exclusive lock beside the file, waiting briefly. Caller must _unlock the handle."""
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
    """Exclusive non-blocking lock; OSError if held. fcntl imported late so --status works without it."""
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    """Release the lock and close the handle, never raising (ValueError = already closed)."""
    with contextlib.suppress(OSError, ValueError):
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(OSError, ValueError):
        handle.close()


def _write(path: Path, leases: dict[str, str]) -> None:
    """Replace the lease file atomically (core imported late: it imports this module)."""
    from backend.poller import core

    core.write_atomic(path, json.dumps(leases, indent=2).encode())
