"""MISO Copilot backend - FastAPI app entry.

Run:  uvicorn backend.main:app --reload --port 8000
Key:  CLAUDE_API_KEY (or ANTHROPIC_API_KEY) in the environment or in
      miso-copilot/.env (gitignored).

Layout:
  config.py   env/.env loading, model + URL constants
  routes/     HTTP endpoints (/ask, /health)
  llm/        Claude client + system prompt
  rag/        (planned) Chroma + LlamaIndex retrieval
  poller/     (planned) 15-min APScheduler poller + JSON->prose summarizers
"""

from fastapi import FastAPI

from backend.routes.ask import router

app = FastAPI(title="MISO Copilot API")
app.include_router(router)
