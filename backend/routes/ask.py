"""/ask and /health endpoints."""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend import security
from backend.llm import claude

router = APIRouter()


class AskRequest(BaseModel):
    # capped so a huge body can't run up embedding/Claude costs
    question: str = Field(min_length=1, max_length=2000)


@router.post("/ask")
def ask(req: AskRequest, request: Request):
    """Answer one question via Claude; maps API failures to clean HTTP errors."""
    ip = security.client_ip(request)
    if not security.allow(ip):
        security.log_request(ip, req.question, "rate_limited", 0)
        raise HTTPException(429, "Too many requests - please slow down a little")

    if claude.client is None:
        raise HTTPException(503, "CLAUDE_API_KEY is not configured")

    started = time.perf_counter()
    outcome = "failed"
    try:
        answer, sources = claude.answer_question(req.question)
        outcome = "answered"
    except anthropic.AuthenticationError:
        raise HTTPException(503, "Claude API key is invalid")
    except anthropic.RateLimitError:
        raise HTTPException(503, "Claude API rate limited, try again shortly")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Claude API error ({e.status_code})")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Could not reach the Claude API")
    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        security.log_request(ip, req.question, outcome, elapsed_ms)

    # fixed EST, not America/New_York: MISO stamps everything in EST year-round
    # (its Snapshot feed said "5:25 PM EST" at 22:27 UTC in September), so a
    # DST-aware zone would read an hour off MISO's own displays all summer
    as_of = datetime.now(ZoneInfo("EST")).strftime("%-I:%M %p EST")
    return {"answer": answer, "sources": sources, "as_of": as_of}


@router.get("/health")
def health():
    """Quick check that the server is up and whether a Claude key is loaded."""
    return {"ok": True, "claude_configured": claude.client is not None}
