"""MISO Copilot backend - FastAPI app entry.

Run:  uvicorn backend.main:app --reload --port 8000
Key:  CLAUDE_API_KEY (or ANTHROPIC_API_KEY) in the environment or in
      miso-copilot/.env (gitignored).

Layout:
  config.py   env/.env loading, model + URL constants
  routes/     HTTP endpoints (/ask, /health)
  llm/        Claude client + system prompt
  rag/        Chroma + LlamaIndex: JSON->prose, upsert, retrieval
  poller/     fetches four MISO endpoints every 5 min and writes their
              JSON verbatim to data/raw/; poller/schedule.py is the
              APScheduler wiring this file starts at boot
"""

# Annotations are strings, so an annotation naming an optional dependency
# cannot be evaluated at import time and cannot fail when it is missing.
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.routes.ask import router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Update Chroma from data/raw/ immediately on boot
    try:
        from backend.poller.core import raw_dir
        from backend.rag.ingest_api import sync_raw_snapshots
        sync_raw_snapshots(raw_dir())
    except Exception as err:
        log.warning("Initial RAG snapshot sync skipped: %s", err)

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
    try:
        from backend.poller import schedule
    except ImportError as e:
        # apscheduler is optional. The API is the demo; the poller is a
        # background convenience, and a missing dependency must not stop
        # /ask and /health from serving. python -m backend.poller still
        # works, so the poller can be run beside the API instead.
        #
        # Name the module that actually failed. This except covers the whole
        # import chain, so blaming apscheduler for a missing requests costs
        # somebody twenty minutes chasing the wrong package.
        missing = e.name or "a dependency"
        log.warning("no poll job registered - %s is not installed. "
                    "Install it with: pip install %s", missing, missing)
        yield
        return

    scheduler = None
    blocked = schedule.poller_blocked()

    if blocked is not None:
        level, reason = blocked
        log.log(level, "no poll job registered - %s", reason)
    else:
        scheduler = schedule.start_scheduler()

    try:
        yield
    finally:
        schedule.stop_scheduler(scheduler)


app = FastAPI(title="MISO Copilot API", lifespan=lifespan)
app.include_router(router)
