'''
LlamaIndex retriever backing the search_docs tool for Claude
'''

from backend.rag.store import get_index


def search_docs(query: str, top_k: int = 4) -> tuple[str, list[dict], str | None]:
    """
    Search Chroma for relevant documents (both live snapshots & static reports).
    Returns: (context_str, sources_list, latest_as_of)
    """
    index = get_index()
    retriever = index.as_retriever(similarity_top_k=top_k)
    nodes = retriever.retrieve(query)

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
