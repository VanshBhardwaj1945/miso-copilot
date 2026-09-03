"""/ask and /health endpoints."""

from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.llm import claude

router = APIRouter()


class AskRequest(BaseModel):
    question: str


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

    # Fixed EST, deliberately, because MISO stamps every value it publishes
    # in fixed EST year-round. Using America/New_York here would print EDT
    # from March to November, so the header would read an hour later than
    # the "as of" time inside the answer and a MISO employee would spot the
    # mismatch immediately. Both zones observe DST identically, so swapping
    # to America/Indiana/Indianapolis fixes nothing - the fix is the fixed
    # offset. Matches the documented contract in frontend/UI_RULES.md.
    as_of = datetime.now(ZoneInfo("EST")).strftime("%-I:%M %p EST")
    return {"answer": answer, "sources": sources, "as_of": as_of}


@router.get("/health")
def health():
    """Quick check that the server is up and whether a Claude key is loaded."""
    return {"ok": True, "claude_configured": claude.client is not None}
