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
The scheduled job itself is run_cycle(), which polls and then re-syncs the
RAG store from the files the poll just wrote.
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

    APScheduler's default thread pool is joined at exit regardless of daemon
    flags, so a cycle stuck on a slow MISO froze the terminal for up to 80 s -
    exactly when someone reaches for Ctrl-C. A plain daemon thread is abandoned.
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
    """Why the poller must not be scheduled, as (log level, reason), or None if it may."""
    if not core.poller_enabled():
        return logging.WARNING, "MISO_POLLER_ENABLED is off"
    directory = core.raw_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return logging.ERROR, f"cannot create {directory} ({e})"
    return None


def resync_rag() -> None:
    """Re-read data/raw/ into Chroma so answers follow the poller.

    Imported late: this module promises apscheduler is its only import-time
    dependency, and the RAG lane drags in chromadb and torch. The seam stays
    the filesystem - this says "re-read the files", it never hands over data.
    """
    from backend.rag.ingest_api import sync_raw_snapshots

    # core.raw_dir(), so MISO_RAW_DIR moves the poller and the ingest together
    results = sync_raw_snapshots(core.raw_dir())
    synced = sum(1 for ok in results.values() if ok)
    if synced == len(results):
        log.info("Chroma re-synced from %d snapshot files", synced)
    else:
        missing = ", ".join(sorted(n for n, ok in results.items() if not ok))
        log.warning("Chroma re-sync incomplete - %d of %d synced, missing: %s",
                    synced, len(results), missing)


def run_cycle() -> None:
    """One poll cycle, then the Chroma re-sync; neither may raise or the schedule dies.

    Separate handlers, so a Chroma failure is never logged as a poll failure.
    The re-sync runs even after a failed poll: data/raw/ still holds the last
    good files and the sync is idempotent.
    """
    try:
        core.poll_once()
    except Exception:
        log.exception("poll cycle failed, staying on schedule")

    try:
        resync_rag()
    except Exception:
        log.exception("Chroma re-sync failed, staying on schedule")


def start_scheduler() -> BackgroundScheduler | None:
    """The started poll scheduler, or None if it would not start - never a reason not to boot."""
    scheduler = None
    try:
        seconds = core.poll_seconds()
        scheduler = BackgroundScheduler(
            executors={"default": DaemonThreadExecutor()})
        # one job (a one-shot plus an interval fires twice at t=0); misfire grace
        # of 300 s so a cycle missed during laptop sleep runs on wake; coalesce
        # and max_instances spelled out because this job must never stack
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
    """Stop a scheduler that may never have been built or started, without raising."""
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        log.debug("scheduler was not running at shutdown", exc_info=True)
