"""Claude client and the answer call.

Raises anthropic.* exceptions to the caller; routes/ask.py maps them to HTTP.
"""

import anthropic

from backend.config import CLAUDE_API_KEY, CONTACT_URL, MISO_HOME_URL, MODEL
from backend.llm.prompts import SYSTEM_PROMPT
from backend.rag.retriever import search_docs

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY) if CLAUDE_API_KEY else None

# True returns the retrieved context verbatim, no Claude call - handy for testing retrieval
FORCE_MOCK = False

def answer_question(question: str) -> tuple[str, list[dict]]:
    """Retrieve context from Chroma and answer via Claude (or Mock Mode)."""
    # retrieve real MISO context from Chroma
    context, sources, _ = search_docs(question)

    # Mock Mode: return the retrieved vector DB context directly
    if FORCE_MOCK or not client:
        context_text = context if context else "No relevant documents found in Chroma DB."
        mock_answer = (
            f"*(Mock Mode - Data retrieved directly from Chroma Vector DB)*\n\n"
            f"{context_text}"
        )
        if not sources:
            sources = [{"title": "misoenergy.org", "url": MISO_HOME_URL}]
        return mock_answer, sources

    # Live Mode: prompt Claude with the retrieved context
    user_content = (
        f"Relevant MISO context retrieved from knowledge base:\n\n"
        f"{context}\n\n"
        f"User Question: {question}\n\n"
        f"Answer concisely using the context above. Cite sources when referencing data."
    ) if context else question

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,   # crosswalk tables and multi-report answers run long
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    if response.stop_reason == "refusal":
        return (
            "I can't help with that question. For assistance, please reach "
            "out to MISO directly.",
            [{"title": "MISO Contact Form", "url": CONTACT_URL}],
        )

    answer = "".join(b.text for b in response.content if b.type == "text")
    if not sources:
        sources = [{"title": "misoenergy.org", "url": MISO_HOME_URL}]

    return answer, sources