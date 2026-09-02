# MISO Copilot

An AI assistant that sits on top of [miso.org](https://www.misoenergy.org) and answers
plain-English questions about MISO's public data - so routine questions get answered in
seconds instead of becoming emails to MISO's CSR and External Affairs teams.

Built for the **Fall 2026 MISO Xtern Challenge** (TechPoint) - Prompt 1: *Intelligent
Navigation of MISO's Public Information*.

```mermaid
flowchart LR
    subgraph FEED["Background feed - every 15 min"]
        POLL["Poller<br/>(APScheduler, 15 min)"] --> MISOAPI["MISO Public APIs<br/>public-api.misoenergy.org<br/>(FuelMix, Load, LMP...)"]
        MISOAPI --> SUMM["JSON → plain-English snapshot<br/>('As of 6:55 PM...')"]
    end

    subgraph INGEST["One-time doc ingestion (polite fetch)"]
        DOCS["MISO docs & reports<br/>(Fact Sheet, Market Reports)"] --> LI["LlamaIndex pipeline<br/>load → CHUNK → embed"]
    end

    subgraph ASK["Question time"]
        USER(["User<br/>(grandma → engineer)"]) --> UI["Chat UI<br/>(Streamlit)"]
        UI --> API["FastAPI backend"]
        API --> CLAUDE["Claude<br/>+ LlamaIndex retriever"]
    end

    SUMM -- "UPSERT: fixed doc_id per endpoint,<br/>replaces old snapshot (no chunking)" --> DB[("Chroma vector DB<br/>live snapshots + docs")]
    LI -- "runs ONCE" --> DB
    CLAUDE -- "search_docs() → top chunks" --> DB
    MISOAPI -. "raw JSON appended" .-> SQL[("SQLite<br/>history + dashboard")]
    CLAUDE -. "answer + 'as of &lt;time&gt;' + source link" .-> USER
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
BACKGROUND (every 15 min)
  Poller → MISO public APIs → JSON→plain-English snapshot → UPSERT into Chroma (fixed IDs)
                             └→ raw JSON appended to SQLite (history + dashboard)

ONE-TIME
  MISO docs & reports → LlamaIndex pipeline (load → chunk → embed) → Chroma

QUESTION TIME
  User → Streamlit chat → FastAPI → Claude + LlamaIndex retriever → Chroma top chunks
       → answer + "as of <time>" + source link
```

Key design decisions:

- **Pull-based, not call-time**: no live API dependency at question time - the demo works
  even if MISO's APIs are down. Tradeoff: answers are ≤15 min stale (stated in every
  answer as "as of \<time\>").
- **UPSERT, never append**: one document per API endpoint with a fixed ID, overwritten
  each poll cycle, so retrieval can never surface a stale snapshot.
- **JSON → prose before embedding**: snapshots are stored as natural-language paragraphs
  ("As of 6:55 PM EST, total generation is 114,136 MW; natural gas leads with…") because
  prose embeds well and raw JSON doesn't.
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

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

The UI currently runs standalone with a stubbed answer function; the FastAPI backend,
poller, and LlamaIndex ingestion land next.

## Repo layout (planned)

```
app.py            # Streamlit chat UI (this is here now)
docs/             # architecture diagram: arch-v1.png + editable .excalidraw source
backend/          # FastAPI app: /ask endpoint, poller, summarizers
ingest/           # one-time LlamaIndex document ingestion
data/             # Chroma persistence + SQLite (gitignored)
```

A full rendering of the architecture lives in [`docs/arch-v1.png`](docs/arch-v1.png);
to edit it, drag [`docs/architecture.excalidraw`](docs/architecture.excalidraw) into
[excalidraw.com](https://excalidraw.com).

## Team

Fall 2026 MISO Xtern Challenge - Prompt 1. Demo day: Sept 11, MISO HQ, Carmel, IN.
