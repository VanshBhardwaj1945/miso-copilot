"""The APScheduler wiring that runs a poll cycle inside the FastAPI process.

Separate from main.py on purpose. main.py has to import cleanly for /ask and
/health to serve at all, and this is the most fragile code in the lane: an
optional third-party dependency, a subclass reaching into APScheduler's
private API, and four failure paths that all have to end in "boot anyway".
None of that belongs in the file the demo depends on.

Importing this module requires apscheduler. That is the contract: the caller
imports it inside try/except ImportError and carries on without a poll job
when it is missing, which is why the executor below can be defined
unconditionally here instead of behind an `if` at module scope.

Entry points are poller_blocked(), start_scheduler() and stop_scheduler().
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from apscheduler.executors.base import BaseExecutor, run_job
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from backend.poller import core

log = logging.getLogger(__name__)


class DaemonThreadExecutor(BaseExecutor):
    """Runs each cycle on a daemon thread, so Ctrl-C is not held up.

    APScheduler's default executor is a concurrent.futures thread pool,
    and the interpreter joins those workers on the way out no matter what
    their daemon flag says - concurrent.futures registers its own
    threading atexit hook that joins them all. A cycle stuck on a slow
    MISO therefore keeps the terminal looking frozen long after the
    lifespan has finished: four links at a 15 second read timeout is up
    to 80 seconds. MISO being slow is exactly when someone reaches for
    Ctrl-C, so nothing may outlive the interpreter here. A plain daemon
    thread is abandoned rather than joined.
    """

    def _do_submit_job(self, job, run_times):
        def run() -> None:
            try:
                events = run_job(job, job._jobstore_alias, run_times,
                                 self._logger.name)
            except BaseException as e:
                self._run_job_error(job.id, e, e.__traceback__)
            else:
                self._run_job_success(job.id, events)

        threading.Thread(target=run, name="miso-poll",
                         daemon=True).start()


def poller_blocked() -> tuple[int, str] | None:
    """Why the poller must not be scheduled, as (log level, reason), or None.

    Two reasons, one path: the poller was switched off, or the data directory
    cannot be created. Both let the app boot with no job registered, so /ask
    and /health keep serving. A third reason - apscheduler is not installed -
    is handled by the caller, because this module cannot be imported at all
    without it.
    """
    if not core.poller_enabled():
        return logging.WARNING, "MISO_POLLER_ENABLED is off"
    directory = core.raw_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # The local, not a second core.raw_dir() call: the message has to name
        # the directory that actually failed, and two calls can disagree.
        return logging.ERROR, f"cannot create {directory} ({e})"
    return None


def run_cycle() -> None:
    """One poll cycle for the scheduler, which never raises out of it.

    A cycle that threw would take the whole schedule down with it, so every
    failure stops here and the next cycle still fires.
    """
    try:
        core.poll_once()
    except Exception:
        log.exception("poll cycle failed, staying on schedule")


def start_scheduler() -> BackgroundScheduler | None:
    """The started poll scheduler, or None if it would not start.

    Every step here can raise - a MISO_POLL_SECONDS that APScheduler rejects,
    a job store that will not take the job, a thread that will not start -
    and none of it is worth refusing to boot over. That is the same bargain
    run_cycle makes for one bad cycle, one level up: the poller is a
    background convenience and /ask and /health are the demo.
    """
    scheduler = None
    try:
        seconds = core.poll_seconds()
        scheduler = BackgroundScheduler(
            executors={"default": DaemonThreadExecutor()})
        # Exactly one job, not a one-shot plus an interval - two jobs fire
        # two cycles at t=0. misfire_grace_time is the one value that is not
        # APScheduler's default: 300 seconds rather than 1, so a cycle that
        # came due while the laptop slept still runs on wake instead of
        # being dropped as too late. coalesce and max_instances match the
        # defaults and are spelled out because this job must never stack -
        # one wake must not fire every cycle it slept through, and a slow
        # cycle must not have the next one start on top of it.
        scheduler.add_job(
            run_cycle,
            IntervalTrigger(seconds=seconds),
            id="miso_poll",
            next_run_time=datetime.now(),
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        scheduler.start()
    except Exception:
        log.exception("no poll job registered - the scheduler would not start")
        stop_scheduler(scheduler)
        return None
    log.info("poller scheduled every %d seconds", seconds)
    return scheduler


def stop_scheduler(scheduler: BackgroundScheduler | None) -> None:
    """Stop a scheduler that may never have been built or started.

    shutdown() is only legal on a running scheduler: it raises outright if
    start() never ran, and it joins a thread that a half-finished start()
    never created. A scheduler that died on the way up must not raise a
    second time on the way down, where the traceback would be about teardown
    rather than the failure that actually mattered.
    """
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        # debug, not exception: a scheduler that never started is the
        # expected case here, and log.exception would force it to ERROR.
        log.debug("scheduler was not running at shutdown", exc_info=True)
