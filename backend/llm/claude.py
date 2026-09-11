"""Claude client and the answer call.

Raises anthropic.* exceptions to the caller; routes/ask.py maps them to HTTP.
"""

import anthropic

from backend.config import CLAUDE_API_KEY, CONTACT_URL, MISO_HOME_URL, MODEL
from backend.llm.prompts import SYSTEM_PROMPT
from backend.rag.retriever import search_docs

# 30 s, not the SDK's 600 s read default. A hung call on the default holds the
# request thread for ten minutes and the UI spins with no way to cancel - the
# frontend fetch has no abort either. A question that has not come back in
# 30 s is not going to be useful on stage anyway.
CLAUDE_TIMEOUT_SECONDS = 30

client = (anthropic.Anthropic(api_key=CLAUDE_API_KEY,
                              timeout=CLAUDE_TIMEOUT_SECONDS)
          if CLAUDE_API_KEY else None)

# True returns the retrieved context verbatim, no Claude call - handy for testing retrieval
FORCE_MOCK = False

def answer_question(question: str) -> tuple[str, list[dict], str | None]:
    """Retrieve context from Chroma and answer via Claude (or Mock Mode).

    Returns: (answer, sources, as_of) - as_of being MISO's own stamp on the
    live data that reached Claude, or None when none did. It is carried out of
    here rather than dropped because the caller was left computing the "as of"
    from its own wall clock, which stamped every answer with the current time
    including the ones read off a document from yesterday.
    """
    # retrieve real MISO context from Chroma
    context, sources, as_of = search_docs(question)

    # Mock Mode: return the retrieved vector DB context directly
    if FORCE_MOCK or not client:
        context_text = context if context else "No relevant documents found in Chroma DB."
        mock_answer = (
            f"*(Mock Mode - Data retrieved directly from Chroma Vector DB)*\n\n"
            f"{context_text}"
        )
        if not sources:
            sources = [{"title": "misoenergy.org", "url": MISO_HOME_URL}]
        return mock_answer, sources, as_of

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
            as_of,
        )

    answer = "".join(b.text for b in response.content if b.type == "text")
    if response.stop_reason == "max_tokens":
        answer += "\n\n*(answer cut short - ask a narrower question for the rest)*"
    if not sources:
        sources = [{"title": "misoenergy.org", "url": MISO_HOME_URL}]

    return answer, sources, as_of