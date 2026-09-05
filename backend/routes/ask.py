"""/ask and /health endpoints."""

from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.llm import claude

router = APIRouter()


class AskRequest(BaseModel):
    # capped so a huge body can't run up embedding/Claude costs
    question: str = Field(min_length=1, max_length=2000)


@router.post("/ask")
def ask(req: AskRequest):
    """Answer one question via Claude; maps API failures to clean HTTP errors."""
    if claude.client is None:
        raise HTTPException(503, "CLAUDE_API_KEY is not configured")

    try:
        answer, sources = claude.answer_question(req.question)
    except anthropic.AuthenticationError:
        raise HTTPException(503, "Claude API key is invalid")
    except anthropic.RateLimitError:
        raise HTTPException(503, "Claude API rate limited, try again shortly")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Claude API error ({e.status_code})")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Could not reach the Claude API")

    # Fixed EST, not a DST-observing zone. MISO publishes every timestamp in
    # fixed EST year-round - its own Snapshot feed stamped "5:25:00 PM EST"
    # at 22:27 UTC on 4 Sep 2026, which is UTC-5 while Eastern was on EDT.
    # America/New_York would print an hour later than MISO's own displays
    # from March to November. Swapping to America/Indiana/Indianapolis fixes
    # nothing: it observes DST identically. The fix is the fixed offset.
    # Matches the contract already written in frontend/UI_RULES.md,
    # frontend/README.md, README.md and AGENTS.md, all of which say EST.
    as_of = datetime.now(ZoneInfo("EST")).strftime("%-I:%M %p EST")
    return {"answer": answer, "sources": sources, "as_of": as_of}


@router.get("/health")
def health():
    """Quick check that the server is up and whether a Claude key is loaded."""
    return {"ok": True, "claude_configured": claude.client is not None}
