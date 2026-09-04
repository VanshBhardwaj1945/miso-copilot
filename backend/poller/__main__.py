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

"""

import argparse
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

# Imported for the side effect of loading .env, so MISO_API_BASE and friends
# work from the file the README trains everyone to use.
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

# The command every "nothing to show you yet" path tells the operator to run.
# One constant because two paths print it and a status screen that offers two
# different commands for the same job looks unrehearsed.
RUN_HINT = ".venv/bin/python -m backend.poller --once"


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


def run_loop() -> NoReturn:
    """Poll now and then forever on the interval. Only Ctrl-C ends it.

    NoReturn, not int: there is no break and no return below, so the only way
    out is KeyboardInterrupt, which main() catches. Each cycle's exit code is
    deliberately dropped - a long-running poller reports through its log, and
    one bad cycle is not a reason to stop polling.
    """
    seconds = core.poll_seconds()
    log.info("polling every %d seconds, Ctrl-C to stop", seconds)
    while True:
        try:
            run_once()
        except Exception:
            # One bad cycle must never end the loop. KeyboardInterrupt is not
            # an Exception, so Ctrl-C still gets through.
            log.exception("cycle raised, continuing")
        # The sleep runs after the cycle, not alongside it, so the real period
        # is the cycle's duration plus this interval and drifts a little later
        # every time. That is the honest reading of "one every
        # MISO_POLL_SECONDS": a few seconds of drift per cycle is invisible
        # against MISO's 5-minute publication grid, and a fixed-rate schedule
        # would have to decide what to do when a cycle overran its own
        # interval - which is exactly the stacking the FastAPI scheduler
        # spells out max_instances=1 to prevent.
        time.sleep(seconds)


# --- --status ---------------------------------------------------------------

def duration_text(seconds: float) -> str:
    """Seconds as a person reads them: 45s, 3m, 2h 5m, 3d 4h."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, _ = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def parse_stamp(value: object) -> datetime | None:
    """An ISO timestamp from the status file, or None if it is unusable.

    Naive timestamps are unusable, not merely unwelcome: age_text subtracts
    what this returns from an offset-aware reference, and mixing an aware and
    a naive datetime raises TypeError. The status file is hand-editable, so a
    naive stamp is a thing that can arrive, and printing "unreadable" beats
    crashing the one command an operator runs to find out what is wrong.
    """
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def age_text(value: object, reference: datetime) -> str:
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


def feed_text(entry: dict[str, Any], endpoint: core.Endpoint,
              reference: datetime) -> str:
    """One phrase describing whether this endpoint's data is actually moving.

    A feed can be frozen while perfectly healthy: MISO keeps serving 200s,
    last_success marches forward, consecutive_failures stays 0, and the
    numbers never change. The gap between last_success and ref_id_changed_at
    is what makes that visible.

    Takes the endpoint row rather than a boolean derived from it, so the
    caller does not have to know that "has a ref_id" means ref_path is set.
    """
    if endpoint.ref_path is None:
        return "n/a - this endpoint has no ref_id"

    changed = parse_stamp(entry.get("ref_id_changed_at"))
    success = parse_stamp(entry.get("last_success"))
    if changed is None or success is None:
        return "unknown - no successful fetch yet"

    since = age_text(entry.get("ref_id_changed_at"), reference)
    if (success - changed).total_seconds() > FROZEN_AFTER_SECONDS:
        return f"FROZEN - ref_id changed {since}"
    return f"moving - ref_id changed {since}"


def _print_header(directory: object, base: str | None = None) -> None:
    """The opening lines every --status path shares, in one place.

    Three paths print this - a full summary, a missing status file and an
    unusable one - and they used to print it three times with small
    variations. Only the full summary has a base_url to show, so that line is
    optional; everything else is identical by construction.
    """
    print("MISO poller status")
    if base is not None:
        print(f"  base_url  {base}")
        if base != core.DEFAULT_BASE_URL:
            # Only what the test above actually establishes. This compares
            # strings, and "not the default string" is not "not MISO":
            # http://, a capitalized host, an explicit :443 and a trailing
            # dot all reach MISO and all fail this comparison. Calling four
            # genuine MISO fetches "NOT MISO" on the operator's screen is
            # worse than saying less. See core.base_is_loopback, which
            # exists because that comparison answered the wrong question.
            print("            not the default base - a stub or an override")
    print(f"  raw_dir   {directory}")


def print_status(status: dict[str, Any], directory: Path,
                 reference: datetime) -> None:
    """Print the whole summary. Reads the given dict, never the network."""
    base = status.get("base_url", "unknown")
    written_dir = status.get("raw_dir", "unknown")

    _print_header(written_dir, base)
    if str(directory) != str(written_dir):
        print(f"            read from {directory} - the two disagree")
    cycle_age = age_text(status.get("cycle_finished_at"), reference)
    print(f"  cycle     finished {cycle_age}  (cycle age, not data age)")
    print()

    endpoints = status.get("endpoints", {})
    print(f"  {'ENDPOINT':<20}{'LAST SUCCESS':<14}{'FAILS':<6}FEED")
    for endpoint in core.ENDPOINTS:
        key = endpoint.key
        entry = endpoints.get(key)
        if not isinstance(entry, dict):
            print(f"  {key:<20}{'no entry':<14}{'-':<6}unknown")
            continue
        success = age_text(entry.get("last_success"), reference)
        failures = entry.get("consecutive_failures", "?")
        feed = feed_text(entry, endpoint, reference)
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
        _print_header(directory)
        print("  No _status.json here yet - no cycle has finished in this")
        print(f"  directory. Run: {RUN_HINT}")
        return EXIT_OK

    status = core.read_status(directory)
    if not status:
        _print_header(directory)
        print(f"  {path.name} exists but is not a usable status file.")
        print(f"  Run: {RUN_HINT}")
        return EXIT_OK

    print_status(status, directory, core.now())
    return EXIT_OK


# --- entry ------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The parsed command line. argv=None reads sys.argv, as argparse does."""
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


def main(argv: list[str] | None = None) -> int:
    """Configure logging, pick a door, and return the process exit code."""
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
            # No value to return: run_loop is NoReturn, so the only way past
            # this call is the Ctrl-C below, and stopping on purpose is a
            # success.
            run_loop()
        except KeyboardInterrupt:
            log.info("stopped")
        return EXIT_OK

    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
