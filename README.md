# MISO Copilot

An AI assistant that sits on top of [miso.org](https://www.misoenergy.org) and answers
plain-English questions about MISO's public data - so routine questions get answered in
seconds instead of becoming emails to MISO's CSR and External Affairs teams.

Built for the **Fall 2026 MISO Xtern Challenge** (TechPoint) - Prompt 1: *Intelligent
Navigation of MISO's Public Information*.

```mermaid
flowchart LR
    subgraph FEED["BACKGROUND FEED - runs every 5 min"]
        POLL["Poller<br/>(APScheduler in FastAPI, 5 min)"] --> MISOAPI["MISO Public APIs<br/>public-api.misoenergy.org<br/>(FuelMix, RealTimeTotalLoad,<br/>Snapshot, WindSolar)"]
        MISOAPI --> RAW[("data/raw/*.json - verbatim<br/>+ _status.json")]
    end

    subgraph RAGLANE["RAG LANE - reads the poller's files"]
        SUMM["JSON → plain-English snapshot<br/>('As of 6:55 PM...')"]
    end

    subgraph ASK["QUESTION TIME"]
        USER(["User"]) --> UI["Chat UI<br/>(React widget; Streamlit backup)"]
        UI --> API["FastAPI backend<br/>(/ask)"]
        API --> CLAUDE["Claude (Opus 5)<br/>answers w/ retrieved ctx"]
        CACHE["(maybe, later)<br/>answer cache for repeated<br/>questions - saves Claude calls"]:::maybe
        API -.-> CACHE
    end

    subgraph INGEST["ONE-TIME DOC INGESTION"]
        DOCS["MISO docs & reports<br/>(Fact Sheet, Market Reports)"] --> LI["LlamaIndex pipeline<br/>load → CHUNK → embed"]
    end

    DB[("Chroma vector DB - the ONLY store<br/>live snapshots (upserted)<br/>+ docs (one-time)")]

    RAW -- "the RAG lane reads the files;<br/>the poller never writes Chroma" --> SUMM
    SUMM -- "UPSERT via LlamaIndex (no chunking -<br/>snapshots are small): one doc per<br/>endpoint, fixed ID - replaces old" --> DB
    CLAUDE -- "search_docs() = LlamaIndex<br/>retriever → top chunks" --> DB
    LI -- "runs ONCE (polite fetch)" --> DB
    CLAUDE -. "answer + 'as of 6:55 PM' + source link" .-> USER

    subgraph NOTES["NOTES"]
        N1["RULE: never APPEND snapshots.<br/>UPSERT/overwrite - or search<br/>retrieves stale data."] ~~~ N2["Tradeoff (own it to judges):<br/>answers about 10 min stale, but ZERO<br/>live dependencies on demo day"] ~~~ N3["Chroma is the only store.<br/>Poller dies? Last snapshot stays -<br/>degrades gracefully, never breaks"] ~~~ N4["No answer? Graceful handoff to<br/>MISO contact form (humans keep<br/>only the hard questions)"]
    end

    classDef note fill:#fff9db,stroke:#f08c00,color:#1e1e1e
    classDef rule fill:#ffc9c9,stroke:#e03131,color:#1e1e1e
    classDef maybe fill:#f1f3f5,stroke:#adb5bd,color:#495057,stroke-dasharray: 5 5
    class N2,N3,N4 note
    class N1 rule
```

## The problem

MISO (the grid operator for 45M people in the central U.S.) publishes enormous amounts of
public data - live grid/market APIs, market reports, planning docs, filings - but none of
it is findable by a normal person. The site search is weak and the Market Reports section
has 11 categories of files with almost no filtering. So everyone from curious citizens to
utility analysts emails MISO's human teams instead.

**MISO Copilot deflects those routine questions**: ask in plain English, get an answer with
a source link, sourced from MISO's own public data.

## Architecture (pull-based RAG)

```
BACKGROUND FEED (every 5 min)
  Poller → MISO public APIs → verbatim JSON written to data/raw/ (+ _status.json)

RAG LANE (separate workstream, reads those files)
  data/raw/*.json → plain-English snapshot → UPSERT into Chroma (fixed IDs)

ONE-TIME
  MISO docs & reports → LlamaIndex pipeline (load → chunk → embed) → Chroma

QUESTION TIME
  User → Streamlit chat → FastAPI → Claude + LlamaIndex retriever → Chroma top chunks
       → answer + "as of <time>" + source link
```

Key design decisions:

- **Pull-based, not call-time**: no live API dependency at question time - the demo works
  even if MISO's APIs are down. Tradeoff: answers are about 10 min stale - the 5-min poll
  cadence plus MISO's own publication lag - stated in every answer as "as of \<time\>".
- **The two lanes meet on disk**: the poller only fetches and writes verbatim JSON to
  `data/raw/`; the RAG lane only reads those files. No shared function and no import
  across the boundary, so either side can be built while the other isn't running.
- **UPSERT, never append** (the RAG lane's job, not the poller's): one document per API
  endpoint with a fixed ID, overwritten each poll cycle, so retrieval can never surface a
  stale snapshot.
- **JSON → prose before embedding** (also the RAG lane's): snapshots are stored as
  natural-language paragraphs ("As of 6:55 PM EST, total generation is 114,136 MW; natural
  gas leads with…") because prose embeds well and raw JSON doesn't.
- **Citations everywhere**: every answer carries its source URL - the link *is* the
  product for "where do I find X" questions.

## Stack

| Piece | Tech | Cost |
|---|---|---|
| Chat UI | Streamlit | $0 |
| Backend | FastAPI | $0 |
| RAG framework | LlamaIndex (ingestion, chunking, retrieval) | $0 |
| Vector store | Chroma (embedded, local persistence) | $0 |
| Embeddings | sentence-transformers (local, all-MiniLM-L6-v2) | $0 |
| LLM | Claude (Anthropic API) | pennies |
| Data | MISO public APIs (`public-api.misoenergy.org`, no auth) + public docs | $0 |

## Data sources

- **Live grid/market data**: [MISO Real-Time Data APIs](https://www.misoenergy.org/markets-and-operations/rtdataapis/)
  - FuelMix, RealTimeTotalLoad, WindSolar, MarketPricing (LMP), Interchange, outages, and
  more. Free JSON, no auth. Polled respectfully (≤1 request/endpoint/minute).
- **Documents**: MISO Fact Sheet, key site pages, Market Reports listings, help-center
  articles - fetched once, politely, at low volume. (Per MISO's guidance: no scraping.)

## Two frontends, one backend (deliberate)

There are two separate UIs that call the same FastAPI backend, and they are NOT
layers of one stack - they are alternatives:

- **React app (`frontend/`) = the demo.** Custom-designed landing page + Copilot
  widget in the MISO palette. This is what judges see.
- **Streamlit app (`app.py`) = testing/backup only.** One Python command, zero
  Node, Streamlit's default look. Used for quick testing and as demo-day
  insurance if the React app can't run. It intentionally does NOT embed or
  wrap the React app - keeping it independent is what makes it a real fallback.

## Quickstart

**React UI (the demo frontend)** - a static MISO-style landing page with the Copilot
panel docked in the bottom-right:

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

**Backend (FastAPI + Claude)**:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo 'CLAUDE_API_KEY=sk-ant-...' > .env    # gitignored - never commit
uvicorn backend.main:app --reload --port 8000
```

**Streamlit UI (fallback / quick testing)**:

```bash
streamlit run app.py
```

The React dev server proxies `/ask` to `localhost:8000` (no CORS setup needed).
Without a key the backend returns 503 and the widget shows a graceful handoff to
MISO's contact form. The full pipeline is live: at boot the backend syncs
`data/raw/` into Chroma and starts the 5-min poller; each question retrieves the
top matching snapshots and Claude (Opus 5) answers from that context with an
"as of \<time\>" stamp and source links. Set `FORCE_MOCK = True` in
`backend/llm/claude.py` to skip Claude and see the raw retrieved context instead
(free, useful for testing retrieval). Python 3.11 recommended - see the platform
notes pinned in `requirements.txt`.

Answers are markdown and both UIs render it: bold/tables/lists, inline links,
code blocks, LaTeX math (KaTeX, real symbols and fractions), and charts - the
backend emits ` ```chart ` fenced JSON blocks (line/bar/area/pie) that the React
UI renders with Recharts and Streamlit renders with its native charts.

## Repo layout

```
frontend/                     # React (Vite) demo UI (see frontend/UI_RULES.md)
  src/copilot/                #   the Copilot chat panel (the product)
  src/fake-landingpage/       #   static MISO-style backdrop page
backend/                      # FastAPI app
  main.py                     #   app entry (uvicorn backend.main:app)
  config.py                   #   .env loading, model + URL constants
  routes/                     #   /ask and /health endpoints
  llm/                        #   Claude client + system prompt
  rag/                        #   Chroma + LlamaIndex: transformers, ingest, retriever
  poller/                     #   5-min poller: verbatim MISO JSON into data/raw/
app.py                        # Streamlit chat UI (fallback; talks to the same backend)
docs/                         # architecture diagram: arch-v1.png + .excalidraw source
tests/                        # poller test suite (627 tests, 100% branch coverage)
data/                         # Chroma persistence, data/raw/ (poller output),
                              #   data/raw.backup/ (demo fallback) - gitignored
```

A full rendering of the architecture lives in [`docs/arch-v1.png`](docs/arch-v1.png);
to edit it, drag [`docs/architecture.excalidraw`](docs/architecture.excalidraw) into
[excalidraw.com](https://excalidraw.com).

## Maybe / later

Ideas we're considering but haven't built:

- **Answer caching for Claude calls** - repeated questions ("what's the fuel mix?")
  could serve a cached answer until the next poll cycle instead of a fresh Claude
  call. Not implemented yet; there is no caching anywhere in the pipeline today.
- **Re-sync Chroma after each poll cycle** - today the vector store only syncs at
  server boot (see `backend/rag/README.md`).
- **Feed the document lane** - `data/docs/` is empty; the Fact Sheet + key report
  pages need one polite fetch and an `ingest_docs.py` run.

## Team

Fall 2026 MISO Xtern Challenge - Prompt 1. Demo day: Sept 11, MISO HQ, Carmel, IN.
