"""Claude client and the answer call.

Raises anthropic.* exceptions to the caller; routes/ask.py maps them to HTTP.
"""

import anthropic

from backend.config import CLAUDE_API_KEY, CONTACT_URL, MISO_HOME_URL, MODEL
from backend.llm.prompts import SYSTEM_PROMPT

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY) if CLAUDE_API_KEY else None


def answer_question(question: str) -> tuple[str, list[dict]]:
    """Ask Claude; return (answer_text, sources)."""
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=2000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": question}],
    )

    if response.stop_reason == "refusal":
        return (
            "I can't help with that question. For assistance, please reach "
            "out to MISO directly.",
            [{"title": "MISO Contact Form", "url": CONTACT_URL}],
        )

    answer = "".join(b.text for b in response.content if b.type == "text")
    return answer, [{"title": "misoenergy.org", "url": MISO_HOME_URL}]
