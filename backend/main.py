"""MISO Copilot backend - FastAPI app entry.

Run:  uvicorn backend.main:app --reload --port 8000
Key:  CLAUDE_API_KEY (or ANTHROPIC_API_KEY) in the environment or in
      miso-copilot/.env (gitignored).

Layout:
  config.py   env/.env loading, model + URL constants
  routes/     HTTP endpoints (/ask, /health)
  llm/        Claude client + system prompt
  rag/        (planned) Chroma + LlamaIndex retrieval
  poller/     fetches four MISO endpoints every 5 min and writes their
              JSON verbatim to data/raw/
"""

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI

try:
    from apscheduler.executors.base import BaseExecutor, run_job
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
except ImportError:
    # apscheduler is optional. The API is the demo; the poller is a
    # background convenience, and a missing dependency must not stop /ask
    # and /health from serving. python -m backend.poller still works.
    BaseExecutor = None
    BackgroundScheduler = None
    IntervalTrigger = None
    run_job = None

from backend.poller import core
from backend.routes.ask import router

log = logging.getLogger(__name__)


if BaseExecutor is None:
    DaemonThreadExecutor = None
else:
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

    Three reasons, one path: apscheduler is not installed, the poller was
    switched off, or the data directory cannot be created. All three let the
    app boot with no job registered, so /ask and /health keep serving.
    """
    if BackgroundScheduler is None:
        return logging.WARNING, ("apscheduler is not installed. Install it "
                                 "with: pip install apscheduler")
    if not core.poller_enabled():
        return logging.WARNING, "MISO_POLLER_ENABLED is off"
    try:
        core.raw_dir().mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return logging.ERROR, f"cannot create {core.raw_dir()} ({e})"
    return None


def run_cycle() -> None:
    """One poll cycle for the scheduler, which never raises out of it.

    A cycle that threw would take the whole schedule down with it, so every
    failure stops here and the next cycle still fires.
    """
    try:
        core.poll_once()
    except Exception:
        log.error("poll cycle failed, staying on schedule", exc_info=True)


def start_scheduler() -> "BackgroundScheduler | None":
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
        # two cycles at t=0. coalesce and misfire_grace_time are spelled out
        # because APScheduler's defaults drop cycles after a laptop sleep
        # and then fire every missed one back to back.
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
        log.error("no poll job registered - the scheduler would not start",
                  exc_info=True)
        stop_scheduler(scheduler)
        return None
    log.info("poller scheduled every %d seconds", seconds)
    return scheduler


def stop_scheduler(scheduler) -> None:
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
        log.debug("scheduler was not running at shutdown", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the poller at boot and stop it at shutdown.

    The first cycle is scheduled rather than awaited: four network fetches in
    front of uvicorn's boot look like a hung process on stage. There is
    therefore a short window after boot where data/raw/ may be empty or
    stale, which the RAG lane already handles.

    A poller that cannot run - apscheduler missing, the poller switched off,
    a data directory that cannot be created, or a scheduler that will not
    start - registers no job and lets the app boot anyway, so /ask and
    /health keep serving.
    """
    scheduler = None
    blocked = poller_blocked()

    if blocked is not None:
        level, reason = blocked
        log.log(level, "no poll job registered - %s", reason)
    else:
        scheduler = start_scheduler()

    try:
        yield
    finally:
        stop_scheduler(scheduler)


app = FastAPI(title="MISO Copilot API", lifespan=lifespan)
app.include_router(router)
