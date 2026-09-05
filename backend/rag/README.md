# backend/rag

Chroma + LlamaIndex retrieval layer. Implemented; the doc corpus is the one
piece still empty.

- `store.py` - Chroma embedded store (persists in `data/chroma/`, gitignored)
  + local embeddings (all-MiniLM-L6-v2). The ONLY store.
- `transformers.py` - each endpoint's raw JSON -> a plain-English snapshot
  paragraph (raw JSON embeds badly; prose matches how people ask).
- `ingest_api.py` - reads `data/raw/*.json` (falls back to `data/raw.backup/`),
  UPSERTs one `Document` per endpoint under a fixed id. Never chunked, never
  appended. Runs at server boot via `main.py` and after every poll cycle via
  `poller/schedule.py`. Takes the raw directory as an argument: both callers
  pass `core.raw_dir()` so `MISO_RAW_DIR` moves the poller and the ingest
  together. Inserts before deleting, so a question arriving mid-write sees a
  duplicate the retriever collapses rather than no snapshot at all.
- `retriever.py` - `search_docs(query)`: top-4 vector search over the same
  collection, returns context + source links + freshest "as of".
- `ingest_docs.py` - one-time chunked ingestion for reference documents.
  Put files in `data/docs/` and run `python -m backend.rag.ingest_docs`.
  **`data/docs/` is currently empty** - the "where do I find X" lane has
  nothing to search until it's fed.

Chroma follows the poller: `schedule.run_cycle()` polls and then calls
`sync_raw_snapshots()`, so answers no longer freeze at boot-time data. The
re-sync runs even when the poll failed - `data/raw/` still holds the last
good files and the sync is idempotent, so a Chroma that started empty fills
itself on the next cycle.
