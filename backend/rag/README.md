# backend/rag

Chroma + LlamaIndex retrieval layer. Implemented; the doc corpus is the one
piece still empty.

- `store.py` - Chroma embedded store (persists in `data/chroma/`, gitignored)
  + local embeddings (all-MiniLM-L6-v2). The ONLY store.
- `transformers.py` - each endpoint's raw JSON -> a plain-English snapshot
  paragraph (raw JSON embeds badly; prose matches how people ask).
- `ingest_api.py` - reads `data/raw/*.json` (falls back to `data/raw.backup/`),
  UPSERTs one `Document` per endpoint under a fixed id. Never chunked, never
  appended. Runs at server boot via `main.py`.
- `retriever.py` - `search_docs(query)`: top-4 vector search over the same
  collection, returns context + source links + freshest "as of".
- `ingest_docs.py` - one-time chunked ingestion for reference documents.
  Put files in `data/docs/` and run `python -m backend.rag.ingest_docs`.
  **`data/docs/` is currently empty** - the "where do I find X" lane has
  nothing to search until it's fed.

Known gap: Chroma only syncs from `data/raw/` at boot. The poller keeps
refreshing the files every 5 min, but nothing re-syncs Chroma after startup -
answers freeze at boot-time data until the server restarts. Fix planned:
call `sync_raw_snapshots()` after each successful poll cycle.
