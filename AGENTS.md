# AGENTS.md

Guidance for AI coding agents (and new humans) working in this repo.

## What this is

**MISO Copilot** - an AI assistant on top of miso.org that answers plain-English
questions about MISO's public data, so routine questions stop landing in the inboxes
of MISO's CSR and External Affairs teams. Built for the Fall 2026 MISO Xtern
Challenge (TechPoint), Prompt 1. Demo day is **Sept 11, 2026** - a working live demo
is worth big points, so never break `streamlit run app.py`.

Read `README.md` first - it holds the architecture diagram, the problem statement,
and the design decisions with their rationale.

## Layout

```
frontend/                   # React (Vite) demo UI
  src/copilot/              #   Copilot chat panel - the product
  src/fake-landingpage/     #   static MISO-style backdrop (no logic, keep it that way)
  UI_RULES.md               #   design rules & locked palette - read before touching UI
backend/                    # FastAPI app (entry: uvicorn backend.main:app)
  config.py                 #   .env loading, model + URL constants
  routes/                   #   /ask and /health endpoints
  llm/                      #   Claude client + system prompt
  rag/                      #   (planned) Chroma + LlamaIndex retrieval - see its README
  poller/                   #   (planned) 15-min poller + summarizers - see its README
app.py                      # Streamlit chat UI (testing/backup ONLY - never the demo;
                            #   independent of frontend/ by design, do not merge them)
docs/                       # architecture diagram (arch-v1.png + .excalidraw source)
ingest/                     # (planned) one-time LlamaIndex document ingestion
data/                       # Chroma persistence - gitignored, never commit
requirements.txt            # deps stay commented until the code that imports them lands
```

## Run / verify

React UI (primary demo frontend):

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

Backend (needs `CLAUDE_API_KEY` in `.env` or the environment; without it, `/ask`
returns 503 and the UI shows the graceful handoff):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

Streamlit UI (fallback):

```bash
streamlit run app.py
```

There is no test suite yet. Minimum verification for any change: the touched UI
boots and a chat message round-trips (stub answer / graceful handoff is fine). If
you add backend code, also verify `uvicorn` boots and `/ask` responds. The React
dev server proxies `/ask` to `localhost:8000` (vite.config.js) - keep that
contract: `POST /ask {question}` → `{answer, sources[{title,url}], as_of}`.
Frontend rule: React + plain CSS only, no UI libraries (react-markdown is the
only pre-approved addition).

## Architecture rules (do not "improve" these away)

The design is **pull-based RAG** - deliberate team decisions, not accidents:

1. **No live MISO API calls at question time.** A background poller (APScheduler
   inside FastAPI, every 15 min) fetches endpoints and writes snapshots into Chroma.
   Question time only reads Chroma. This keeps the demo alive even if MISO's APIs go
   down. Answers may be ≤15 min stale - that's accepted and disclosed.
2. **Chroma is the ONLY store.** No SQLite, no extra databases. Question logging, if
   added, is a plain JSONL file.
3. **UPSERT, never append.** One document per API endpoint with a **fixed doc_id**,
   overwritten each poll cycle. Appending snapshots makes vector search retrieve
   stale near-duplicates.
4. **JSON → prose before embedding.** Snapshots are stored as natural-language
   paragraphs ("As of 6:55 PM EST, total generation is 114,136 MW…"), never raw
   JSON. Raw numbers embed terribly.
5. **Timestamp in every snapshot**, and the system prompt must force "as of <time>"
   into answers - staleness stays visible, never hidden.
6. **Citations on every doc-lane answer.** The source URL is the product.
7. **RAG framework is LlamaIndex** (the Python library). Docs: load →
   `SentenceSplitter` chunks (~512 tokens, ~50 overlap) → embed → Chroma. Poller
   snapshots: single small `Document` with fixed `doc_id`, **no chunking**.
   Query: `index.as_retriever(similarity_top_k=4)` over the same collection.
8. **Embeddings are local** (sentence-transformers all-MiniLM-L6-v2). LLM is Claude
   via the Anthropic API with a single tool, `search_docs(query)`.
9. **Out-of-scope questions get a graceful handoff** to MISO's contact form - never
   a hallucinated answer.

## Hard external constraints (from MISO mentors - violating these can get us banned)

- **Do NOT scrape miso.org.** It has anti-scraping protection; scrapers get
  IP-banned. Use the public APIs and politely-fetched documents only.
- **Rate limit: max ~1 request per endpoint per minute** against
  `https://public-api.misoenergy.org` (free JSON, no auth). The 15-min poller is
  already far under this - keep it that way.
- API values come back as **strings**, not numbers - parse before doing math.
- MISO is mid-migration to the MISO Data Exchange API (pricing/load endpoints moving
  after Sept 30) - don't hard-fail if an endpoint disappears; degrade to the last
  stored snapshot.

## Secrets & hygiene

- The Anthropic API key lives in `.env` (gitignored). **Never commit keys**, never
  print them in logs or error messages.
- `data/` and `chroma_db/` are local stores - gitignored, never commit.

## Conventions

- Python, plain and readable - this is a hackathon repo judged on clarity of
  thought, not framework sophistication. Prefer a small readable function over an
  abstraction.
- Keep `requirements.txt` honest: dependencies stay commented out until the code
  that imports them lands.
- Docstrings state what a module does and what's stubbed/pending (see `app.py`).
- If you change the architecture picture, update both `docs/architecture.excalidraw`
  (source) and `docs/arch-v1.png` (render), plus the mermaid diagram in `README.md`.
- Update `README.md` when planned pieces (backend/, ingest/) become real.

## Definition of done for a change

1. App still boots (`streamlit run app.py`).
2. No secrets, no `data/` artifacts, no `__pycache__` in the commit.
3. README/docs updated if behavior or layout changed.
4. Any new answer path includes a source URL and an "as of <time>" stamp.
