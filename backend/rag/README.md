# backend/rag (planned)

Chroma + LlamaIndex retrieval layer, per the architecture in the root README:

- Chroma embedded vector store (local persistence in `data/`, gitignored) -
  the ONLY store.
- LlamaIndex retriever (`index.as_retriever(similarity_top_k=4)`) backing the
  single `search_docs(query)` tool given to Claude.
- Doc feed: load -> `SentenceSplitter` chunks (~512 tokens, ~50 overlap) ->
  embed (sentence-transformers all-MiniLM-L6-v2) -> Chroma.
- Snapshot feed (from `../poller/`): one small `Document` per API endpoint
  with a fixed `doc_id`, UPSERTed (delete + insert), never chunked, never
  appended.
