"""Command line entry for the poller: python -m backend.poller.

Three doors, mutually exclusive, of which --once is the default:

  --once     run one cycle, then exit with a code that says what happened
  --loop     one cycle now, then one every MISO_POLL_SECONDS, forever
  --status   print what data/raw/_status.json says and exit, touching no
             network

Exit codes for --once are the contract, not decoration: 0 means at least one
endpoint succeeded, 2 means every endpoint was skipped by the rate guard so
nothing was even attempted, and 1 means everything that was attempted failed.
A guard skip is neither success nor failure and folding it into either hides
the one thing worth knowing.

Logging is configured here and only here. Importing the package must never
reconfigure a host application's logging.

See the specification, sections 7.1, 8.3 and 8.4.
"""

import argparse
import logging
import time
from datetime import datetime
from pathlib import Path

# Imported for the side effect of loading .env, so MISO_API_BASE and friends
# work from the file the README trains everyone to use (specification 8.6).
import backend.config  # noqa: F401
from backend.poller import core

log = logging.getLogger("backend.poller")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_SKIPPED = 2

# How far last_success may outrun ref_id_changed_at before we call the feed
# frozen. MISO publishes on a 5-minute grid, so three missed publications is
# a real stall rather than a slow interval.
FROZEN_AFTER_SECONDS = 900


# --- one cycle --------------------------------------------------------------

def run_once() -> int:
    """Run a single cycle and return the process exit code."""
    try:
        status = core.poll_once()
    except OSError as e:
        # data/raw/ could not be created, or _status.json could not be
        # written. Either way this cycle produced nothing readable.
        log.error("cycle could not complete: %s", e)
        return EXIT_FAILED

    if status.get("skipped"):
        log.warning("nothing attempted - every endpoint is inside the "
                    "rate guard's 60 second window")
        return EXIT_SKIPPED

    succeeded = core.succeeded_count(status)
    total = len(core.ENDPOINTS)
    if succeeded == 0:
        log.error("all %d endpoints failed", total)
        return EXIT_FAILED

    log.info("cycle done - %d of %d endpoints succeeded", succeeded, total)
    return EXIT_OK


def run_loop() -> int:
    """Poll now and then forever on the interval. Only Ctrl-C ends it."""
    seconds = core.poll_seconds()
    log.info("polling every %d seconds, Ctrl-C to stop", seconds)
    while True:
        try:
            run_once()
        except Exception:
            # One bad cycle must never end the loop. KeyboardInterrupt is not
            # an Exception, so Ctrl-C still gets through.
            log.error("cycle raised, continuing", exc_info=True)
        time.sleep(seconds)


# --- --status ---------------------------------------------------------------

def duration_text(seconds: float) -> str:
    """Seconds as a person reads them: 45s, 3m, 2h 5m, 3d 4h."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def parse_stamp(value) -> datetime | None:
    """An ISO timestamp from the status file, or None if it is unusable."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def age_text(value, reference: datetime) -> str:
    """How long ago a stored timestamp was, phrased for a terminal."""
    if value is None:
        return "never"
    moment = parse_stamp(value)
    if moment is None:
        return "unreadable"
    seconds = (reference - moment).total_seconds()
    if seconds < 0:
        return "in the future"
    return duration_text(seconds) + " ago"


def feed_text(entry: dict, has_ref_id: bool, reference: datetime) -> str:
    """One phrase describing whether this endpoint's data is actually moving.

    A feed can be frozen while perfectly healthy: MISO keeps serving 200s,
    last_success marches forward, consecutive_failures stays 0, and the
    numbers never change. The gap between last_success and ref_id_changed_at
    is what makes that visible.
    """
    if not has_ref_id:
        return "n/a - this endpoint has no ref_id"

    changed = parse_stamp(entry.get("ref_id_changed_at"))
    success = parse_stamp(entry.get("last_success"))
    if changed is None or success is None:
        return "unknown - no successful fetch yet"

    since = age_text(entry.get("ref_id_changed_at"), reference)
    if (success - changed).total_seconds() > FROZEN_AFTER_SECONDS:
        return f"FROZEN - ref_id changed {since}"
    return f"moving - ref_id changed {since}"


def print_status(status: dict, directory: Path, reference: datetime) -> None:
    """Print the whole summary. Reads the given dict, never the network."""
    base = status.get("base_url", "unknown")
    written_dir = status.get("raw_dir", "unknown")

    print("MISO poller status")
    print(f"  base_url  {base}")
    if base != core.DEFAULT_BASE_URL:
        print("            NOT MISO - this status came from a stub or override")
    print(f"  raw_dir   {written_dir}")
    if str(directory) != str(written_dir):
        print(f"            read from {directory} - the two disagree")
    cycle_age = age_text(status.get("cycle_finished_at"), reference)
    print(f"  cycle     finished {cycle_age}  (cycle age, not data age)")
    print()

    endpoints = status.get("endpoints", {})
    print(f"  {'ENDPOINT':<20}{'LAST SUCCESS':<14}{'FAILS':<6}FEED")
    for endpoint in core.ENDPOINTS:
        key = endpoint["key"]
        entry = endpoints.get(key)
        if not isinstance(entry, dict):
            print(f"  {key:<20}{'no entry':<14}{'-':<6}unknown")
            continue
        success = age_text(entry.get("last_success"), reference)
        failures = entry.get("consecutive_failures", "?")
        feed = feed_text(entry, endpoint["ref_path"] is not None, reference)
        print(f"  {key:<20}{success:<14}{str(failures):<6}{feed}")
        error = entry.get("last_error")
        if error:
            print(f"  {'':<20}last error: {error}")
    print()
    print("  Read from _status.json only. No network request was made.")


def run_status() -> int:
    """Print the summary, or say plainly why there is nothing to print."""
    directory = core.raw_dir()
    path = core.status_path(directory)
    if not path.exists():
        print("MISO poller status")
        print(f"  raw_dir   {directory}")
        print("  No _status.json here yet - no cycle has finished in this")
        print("  directory. Run: python -m backend.poller --once")
        return EXIT_OK

    status = core.read_status(directory)
    if not status:
        print("MISO poller status")
        print(f"  raw_dir   {directory}")
        print(f"  {path.name} exists but is not a usable status file.")
        print("  Run: python -m backend.poller --once")
        return EXIT_OK

    print_status(status, directory, core.now())
    return EXIT_OK


# --- entry ------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.poller",
        description="Fetch four MISO JSON endpoints into data/raw/.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="run one cycle and exit (the default)")
    mode.add_argument("--loop", action="store_true",
                      help="run a cycle now, then one every MISO_POLL_SECONDS")
    mode.add_argument("--status", action="store_true",
                      help="print the last cycle's status and exit, no network")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    # Only here, never at import. INFO rather than DEBUG so that a --once run
    # is self-evidencing: the root logger defaults to WARNING, so DEBUG lines
    # would make a successful run print nothing at all.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.status:
        return run_status()

    if args.loop:
        try:
            return run_loop()
        except KeyboardInterrupt:
            log.info("stopped")
            return EXIT_OK

    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
