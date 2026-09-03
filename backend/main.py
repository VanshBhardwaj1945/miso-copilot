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
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
except ImportError:
    # apscheduler is optional. The API is the demo; the poller is a
    # background convenience, and a missing dependency must not stop /ask
    # and /health from serving. python -m backend.poller still works.
    BackgroundScheduler = None
    IntervalTrigger = None

from backend.poller import core
from backend.routes.ask import router

log = logging.getLogger(__name__)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the poller at boot and stop it at shutdown.

    The first cycle is scheduled rather than awaited: four network fetches in
    front of uvicorn's boot look like a hung process on stage. There is
    therefore a short window after boot where data/raw/ may be empty or
    stale, which the RAG lane already handles.

    A poller that cannot run - apscheduler missing, the poller switched off,
    or a data directory that cannot be created - registers no job and lets
    the app boot anyway, so /ask and /health keep serving.
    """
    scheduler = None
    blocked = poller_blocked()

    if blocked is not None:
        level, reason = blocked
        log.log(level, "no poll job registered - %s", reason)
    else:
        seconds = core.poll_seconds()
        scheduler = BackgroundScheduler()
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
        log.info("poller scheduled every %d seconds", seconds)

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(title="MISO Copilot API", lifespan=lifespan)
app.include_router(router)
