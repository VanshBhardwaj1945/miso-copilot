"""LlamaIndex retriever backing the search_docs step of every answer.

Three lanes share one Chroma collection: four live snapshots, eleven settled
Data Exchange market days, and a few hundred reference-document chunks.
Searched together, a wordy question about "grid conditions" fills the top-k
with Fact Sheet chunks and the live numbers never reach Claude. So each lane
is searched on its own and the results are handed over, snapshots first -
every lane keeps a seat at the table and Claude decides what the question
actually needs.
"""

from llama_index.core.vector_stores import (
    FilterOperator,
    MetadataFilter,
    MetadataFilters,
)

from backend.rag.store import get_index

# Three lanes, one budget each. A shared budget was tried and measured: the
# eleven settled Data Exchange documents share an opening sentence, embed as a
# near-duplicate block, and took all four seats on "what is MISO's total
# generation right now" - answering from yesterday and stamping it with
# yesterday's time, while the live feed that knew the answer was cut. Widening
# the shared budget only admits more of the block; separating the lanes is what
# guarantees the live feeds a seat.
# The two snapshot budgets are sized against measured recall, not intuition.
# Two live seats cut the fuel mix from "what share of load is wind serving
# right now", which needs three of the four feeds at once; the whole live
# corpus is four short paragraphs, so a third seat costs almost nothing. Two
# settled seats put the day-ahead fuel mix at rank 3 behind its real-time
# twin, which reads nearly identically to an embedding; a third recovers it.
LIVE_TOP_K = 3      # four display feeds: what is happening now
SETTLED_TOP_K = 3   # eleven Data Exchange feeds: a completed market day, by region
DOC_TOP_K = 4       # the 512-token chunks, same budget as before the doc lane


def _retrieve(index, query: str, doc_type: str, top_k: int) -> list:
    """Top-k nodes of one lane, selected by the doc_type metadata every chunk carries."""
    only_this_lane = MetadataFilters(filters=[
        MetadataFilter(key="doc_type", value=doc_type, operator=FilterOperator.EQ),
    ])
    return index.as_retriever(similarity_top_k=top_k,
                              filters=only_this_lane).retrieve(query)


def search_docs(query: str, top_k: int = DOC_TOP_K) -> tuple[str, list[dict], str | None]:
    """
    Search Chroma for relevant context (live snapshots & reference documents).
    Returns: (context_str, sources_list, latest_as_of)
    """
    index = get_index()
    # live first: a question that can be answered by both should read the
    # current numbers before yesterday's
    nodes = (_retrieve(index, query, "live_snapshot", LIVE_TOP_K)
             + _retrieve(index, query, "settled_market_day", SETTLED_TOP_K)
             + _retrieve(index, query, "reference_doc", top_k))

    context_blocks = []
    sources = []
    latest_as_of = None

    seen_endpoints = set()

    for node in nodes:
        meta = node.metadata or {}
        endpoint = meta.get("endpoint")
        # both snapshot lanes keep one document per endpoint; two chunks of one
        # endpoint would be the same numbers twice
        if meta.get("doc_type") in ("live_snapshot", "settled_market_day") and endpoint:
            if endpoint in seen_endpoints:
                continue
            seen_endpoints.add(endpoint)

        context_blocks.append(node.get_content().strip())

        url = meta.get("source_url")
        title = meta.get("title", "MISO Resource")
        if url and not any(s["url"] == url for s in sources):
            sources.append({"title": title, "url": url})

        # live only, deliberately. A settled market day's as-of is yesterday,
        # and stamping an answer with it says the whole answer is a day old
        # even when the live feeds supplied it - which is how "total generation
        # right now" came back stamped 2026-09-09.
        if meta.get("doc_type") == "live_snapshot" and meta.get("as_of") and not latest_as_of:
            latest_as_of = meta.get("as_of")

    context_str = "\n\n---\n\n".join(context_blocks)
    return context_str, sources, latest_as_of
