# backend/rag (planned)

Chroma + LlamaIndex retrieval layer, per the architecture in the root README:

- Chroma embedded vector store (local persistence in `data/`, gitignored) -
  the ONLY store. `data/` has two tenants: this persistence, and the
  poller's `data/raw/` (plus its `data/raw.backup/` demo fallback), which
  are files nothing queries.
- LlamaIndex retriever (`index.as_retriever(similarity_top_k=4)`) backing the
  single `search_docs(query)` tool given to Claude.
- Doc feed: load -> `SentenceSplitter` chunks (~512 tokens, ~50 overlap) ->
  embed (sentence-transformers all-MiniLM-L6-v2) -> Chroma.
- Snapshot feed, read from `data/raw/*.json` (verbatim MISO JSON the
  poller writes, with `data/raw/_status.json` beside it for freshness and
  health): turn each endpoint's JSON into prose here, then store it as one
  small `Document` per endpoint with a fixed `doc_id`, UPSERTed (delete +
  insert), never chunked, never appended. The poller writes no prose and no
  Chroma - all of that is this lane's.
