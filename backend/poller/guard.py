"""Per-link rate guard - makes MISO's one-request-per-minute rule mechanical.

MISO's published limit is one request per link per minute, and they IP-ban
scrapers. This module is the only thing that makes that limit true no matter
how the poller was started: a fresh `uvicorn --reload` on every file save, a
crash-restart loop, a stray `--once` alongside the scheduler, or a cycle that
overran a minute because MISO was slow.

The mechanism is a lease claimed BEFORE each request, not after:

    if claim("https://public-api.misoenergy.org/api/FuelMix"):
        requests.get(...)

`claim` takes its own timestamp, under its own lock, and returns with the lock
released. Both halves of that sentence are load-bearing and both were once
wrong - see the docstring on `claim`.

State lives at ~/.cache/miso-copilot/rate-guard.json, deliberately outside the
repo and outside data/, so that no MISO_RAW_DIR override and no `rm -rf data/`
can disable it.

See the specification, section 4.3.
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


class GuardUnreadable(Exception):
    """The lease file exists but its contents cannot be trusted.

    Distinct from a missing file, which simply means first run. This one is
    fatal to the cycle: specification 4.3 says a guard that cannot read its
    own state fails closed.
    """


def guard_path() -> Path:
    """Where the lease file lives. Honors XDG_CACHE_HOME, defaults to ~/.cache."""
    cache = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(cache) / "miso-copilot" / "rate-guard.json"


def _now() -> datetime:
    """Our wall clock, offset-aware. One definition, shared with core.

    core is imported here rather than at module scope because core imports
    this module; a deferred import keeps the dependency one-directional.
    """
    from backend.poller import core

    return core.now()


def _read(path: Path) -> dict:
    """Load the lease file. Missing means first run; unusable is an error.

    The two cases are not the same and must not be collapsed. A missing file
    is the normal cold start and proceeds with an empty set of leases. A file
    that exists and cannot be read, or that holds something other than a JSON
    object, means every lease we wrote is invisible to us - returning `{}`
    there would forget all four leases and fetch all four links immediately,
    which is exactly the burst the guard exists to prevent.
    """
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except OSError as e:
        # strerror, not the whole OSError: the caller logs the path itself.
        raise GuardUnreadable(f"cannot read lease file ({e.strerror})") from e
    except (ValueError, RecursionError) as e:
        # RecursionError alongside ValueError because json raises it, not
        # ValueError, on a deeply nested file. Either way the leases are
        # unreadable, and an unreadable guard fails closed.
        raise GuardUnreadable(f"lease file is not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise GuardUnreadable("lease file is not a JSON object")
    return data


def _decide(stored: str | None, now: datetime) -> tuple[bool, str | None]:
    """Should this request proceed? Returns (allowed, warning to log).

    An abnormal value *inside* an otherwise readable file proceeds rather
    than blocks: one garbled timestamp among good ones should not stop all
    polling, and a single extra request is a far smaller problem than a dead
    poller nobody noticed. That reasoning covers bad values only. A file we
    cannot read at all is a different question and is answered in `_read`,
    which fails closed as specification 4.3 requires.
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

    # A lease well into the future means the clock moved backward (an NTP
    # step, a laptop waking, a lease file copied from another machine).
    # Treating "in the future" as "recent" would block every request until
    # wall-clock time caught up, which is a silent total outage.
    #
    # The tolerance is what keeps this from being a hole. A lease a few
    # milliseconds ahead is not a clock change; it is another process that
    # claimed this link moments ago, and calling that "the future" let both
    # processes fetch the same link seconds apart.
    if age < -CLOCK_BACKWARD_TOLERANCE:
        return True, f"lease timestamp {stored!r} is in the future, proceeding"

    if age < timedelta(seconds=MIN_SECONDS_BETWEEN):
        return False, None

    if age > STALE_AFTER:
        return True, f"no request to this link for {age}, poller was down"

    return True, None


def claim(url: str) -> bool:
    """Claim the right to request `url`. True when the request may proceed.

    Two properties make this correct, and each replaces something that was
    measurably broken:

    The timestamp is read HERE, after the lock is held, not passed in by the
    caller. A caller-supplied `now` is read before the lock, so the process
    that wins the lock second compares against a moment older than the lease
    the winner just wrote. That reads as a negative age - a clock jump - and
    both processes fetch the same link. Racing four processes on one link
    used to produce two or three winners.

    The lock covers the read, the decide, and the write, and nothing else.
    The caller fetches after this function returns, with the lock already
    released. Holding one global lock across a network request made every
    other link wait out a fetch it had nothing to do with, and a fetch longer
    than LOCK_TIMEOUT_SECONDS (routine at timeout=(5, 15)) made the waiter
    record a rate-guard skip for a link it never requested.

    The lease is written before the request is issued, so a process killed
    mid-fetch still leaves its claim behind. A denied claim writes nothing.
    """
    path = guard_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Fail closed. A poller that cannot prove it is under the limit does
        # not fetch - better a stale demo than an IP ban.
        log.error("rate guard: cannot create %s (%s), refusing to fetch", path.parent, e)
        return False

    try:
        handle = _lock(path)
    except OSError as e:
        log.error("rate guard: cannot lock %s (%s), refusing to fetch", path, e)
        return False

    try:
        try:
            leases = _read(path)
        except GuardUnreadable as e:
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

    Public so that core can serialize its own read-merge-write of
    _status.json without growing a second flock implementation. The caller
    picks `path`, and the status file's lock is therefore a different file
    from the lease file's. One lock for both would make every status write
    queue behind whichever process is currently claiming a lease, coupling
    two things that have no reason to wait on each other.

    A lock we cannot take is logged and the body runs anyway. Refusing to
    write the status file at all is worse than the interleaving this
    serializes, and it is what happened before this lock existed.

    Never hold this across a network call. LOCK_TIMEOUT_SECONDS is shorter
    than one fetch timeout, so a waiter would give up and lose its lock.
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
    """Take an exclusive lock beside the given file, waiting briefly if held."""
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
