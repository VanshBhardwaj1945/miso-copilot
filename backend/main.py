"""MISO Copilot backend - FastAPI app entry.

Run:  uvicorn backend.main:app --reload --port 8000
Key:  CLAUDE_API_KEY (or ANTHROPIC_API_KEY) in the environment or in
      miso-copilot/.env (gitignored).

Layout:
  config.py   env/.env loading, model + URL constants
  routes/     HTTP endpoints (/ask, /health)
  llm/        Claude client + system prompt
  rag/        Chroma + LlamaIndex: snapshots, document corpus, crosswalk,
              retrieval
  poller/     fetches four MISO endpoints every 5 min, writes their JSON
              verbatim to data/raw/, then asks the RAG lane to re-sync
"""

# Annotations are strings, so an annotation naming an optional dependency
# cannot be evaluated at import time and cannot fail when it is missing.
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.routes.ask import router
from backend.routes.crosswalk import router as crosswalk_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # sync Chroma from data/raw/ before the first request; the poller keeps
    # it fresh from here (schedule.run_cycle)
    try:
        from backend.poller.core import raw_dir
        from backend.rag.ingest_api import sync_raw_snapshots
        sync_raw_snapshots(raw_dir())
    except Exception as err:
        log.warning("Initial RAG snapshot sync skipped: %s", err)

    # Start the poller, stop it at shutdown. Every failure below - apscheduler
    # missing, poller switched off, no data dir, scheduler won't start -
    # registers no job and boots anyway: /ask and /health are the demo.
    try:
        from backend.poller import schedule
    except ImportError as e:
        # name the module that actually failed, not always apscheduler
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
app.include_router(crosswalk_router)
