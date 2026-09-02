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

    as_of = datetime.now(ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")
    return {"answer": answer, "sources": sources, "as_of": as_of}


@router.get("/health")
def health():
    return {"ok": True, "claude_configured": claude.client is not None}
