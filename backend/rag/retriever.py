"""LlamaIndex retriever backing the search_docs step of every answer.

Two lanes share one Chroma collection: four small live snapshots and a few
dozen reference-document chunks. Searched together, a wordy question about
"grid conditions" fills the top-k with Fact Sheet chunks and the live numbers
never reach Claude. So each lane is searched on its own and both results are
handed over, snapshots first - every lane keeps a seat at the table and Claude
decides what the question actually needs.
"""

from llama_index.core.vector_stores import (
    FilterOperator,
    MetadataFilter,
    MetadataFilters,
)

from backend.rag.store import get_index

LIVE_TOP_K = 2   # there are only four snapshots; two covers any one question
DOC_TOP_K = 4    # the 512-token chunks, same budget as before the doc lane


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
    nodes = (_retrieve(index, query, "live_snapshot", LIVE_TOP_K)
             + _retrieve(index, query, "reference_doc", top_k))

    context_blocks = []
    sources = []
    latest_as_of = None

    seen_endpoints = set()

    for node in nodes:
        meta = node.metadata or {}
        endpoint = meta.get("endpoint")
        if meta.get("doc_type") == "live_snapshot" and endpoint:
            if endpoint in seen_endpoints:
                continue
            seen_endpoints.add(endpoint)

        context_blocks.append(node.get_content().strip())

        url = meta.get("source_url")
        title = meta.get("title", "MISO Resource")
        if url and not any(s["url"] == url for s in sources):
            sources.append({"title": title, "url": url})

        if meta.get("doc_type") == "live_snapshot" and meta.get("as_of") and not latest_as_of:
            latest_as_of = meta.get("as_of")

    context_str = "\n\n---\n\n".join(context_blocks)
    return context_str, sources, latest_as_of
