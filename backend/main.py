"""MISO Copilot backend - FastAPI /ask endpoint.

Basic version: every question goes straight to Claude with a MISO system
prompt. The RAG store (Chroma + LlamaIndex) and the 15-min poller land next.

Run:  uvicorn backend.main:app --reload --port 8000
Key:  CLAUDE_API_KEY (or ANTHROPIC_API_KEY) in the environment or in
      miso-copilot/.env (gitignored).
"""

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MODEL = "claude-opus-5"

CONTACT_URL = "https://www.misoenergy.org/about/contact-us/"
REALTIME_URL = (
    "https://www.misoenergy.org/markets-and-operations/"
    "real-time--market-data/real-time-displays/"
)

SYSTEM_PROMPT = """You are MISO Copilot, an assistant embedded on MISO's public \
website that answers plain-English questions about MISO for anyone - from a \
curious citizen to a grid engineer.

Facts about MISO:
- MISO is the Midcontinent Independent System Operator: an independent, \
non-profit, member-based organization that operates the electricity grid and \
wholesale energy markets across 15 U.S. states and the Canadian province of \
Manitoba, serving about 45 million people.
- MISO does NOT generate power or own transmission lines. It coordinates and \
monitors the grid and runs the markets - think "air traffic control for \
electricity."
- Headquarters: Carmel, Indiana, with operations centers in Carmel and Eagan, \
Minnesota.
- Members include utilities, transmission owners, independent power producers, \
and other market participants. MISO is regulated by FERC.
- Key public resources on misoenergy.org: Real-Time Displays (live fuel mix, \
load, prices), Market Reports (historical LMP/pricing, load, settlements, \
outages), the generator interconnection queue, transmission planning (MTEP), \
resource adequacy and the Planning Resource Auction, the tariff, FERC filings, \
and seasonal reliability assessments.

Rules:
- Answer in plain English at the level of the question. Be concise: a few \
sentences to two short paragraphs.
- You do not yet have live data access. For current numbers (fuel mix, load, \
prices), give a brief explanation and point to the Real-Time Displays page: \
""" + REALTIME_URL + """
- Point people to the relevant misoenergy.org section when it helps.
- Never invent numbers, statistics, or document names. If you don't know or \
the question is out of scope, say so and point to MISO's contact page: \
""" + CONTACT_URL

app = FastAPI(title="MISO Copilot API")


def _load_dotenv() -> None:
    """Tiny .env loader so we don't need python-dotenv."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()
_api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=_api_key) if _api_key else None


class AskRequest(BaseModel):
    question: str


@app.post("/ask")
def ask(req: AskRequest):
    if client is None:
        raise HTTPException(503, "CLAUDE_API_KEY is not configured")

    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": req.question}],
        )
    except anthropic.AuthenticationError:
        raise HTTPException(503, "Claude API key is invalid")
    except anthropic.RateLimitError:
        raise HTTPException(503, "Claude API rate limited, try again shortly")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Claude API error ({e.status_code})")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Could not reach the Claude API")

    if response.stop_reason == "refusal":
        answer = (
            "I can't help with that question. For assistance, please reach "
            "out to MISO directly."
        )
        sources = [{"title": "MISO Contact Form", "url": CONTACT_URL}]
    else:
        answer = "".join(b.text for b in response.content if b.type == "text")
        sources = [{"title": "misoenergy.org", "url": "https://www.misoenergy.org"}]

    as_of = datetime.now(ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")
    return {"answer": answer, "sources": sources, "as_of": as_of}


@app.get("/health")
def health():
    return {"ok": True, "claude_configured": client is not None}
