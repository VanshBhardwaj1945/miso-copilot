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
  routes/                   #   /ask, /health, and /crosswalk.csv
  llm/                      #   Claude client + system prompt
  rag/                      #   Chroma + LlamaIndex: transformers, both ingests, retriever;
                            #   doc_sources.json = the document corpus (URLs),
                            #   crosswalk.json = report->API mappings, build_crosswalk.py
                            #   drafts them with Claude and validates against the spec
  poller/                   #   5-min poller: verbatim JSON to data/raw/ - see README
app.py                      # Streamlit chat UI (testing/backup ONLY - never the demo;
                            #   independent of frontend/ by design, do not merge them)
tests/                      # pytest suite for backend/poller/, plus the MISO stub
docs/                       # architecture diagrams: architecture.svg (README) +
                            #   architecture-detailed.svg/.png (full version) +
                            #   terraform-architecture.svg/.png (cloud reference)
infra/                      # validated Terraform sketch of a future cloud
                            #   deployment - reference only, never applied
data/                       # gitignored, never commit. Chroma persistence, plus
                            #   data/raw/ (poller output), data/raw.backup/
                            #   (demo fallback), data/docs/ (fetched corpus) and
                            #   data/specs/ (Data Exchange OpenAPI specs)
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

Two rules for running the backend, because the poller lives inside it:

- **One worker.** Never `--workers N` - the poller's scheduler starts in every
  worker, and N schedulers contending on one lease file is waste nobody should
  have to debug on demo morning.
- **One machine at a time.** The rate guard is a local file and cannot see
  another laptop. On demo day the whole team is behind one public IP in MISO's
  own building.

`--reload` is safe and stays: the guard claims a per-link lease before each
request, so a reload mid-cycle cannot double-hit an endpoint. Do not remove it
thinking it breaks the rate limit. To run the API without the poller at all,
set `MISO_POLLER_ENABLED=0`.

Poller commands (`--once`, `--loop`, `--status`) are in
`backend/poller/README.md`. `python -m backend.poller --status` is the
one-command answer to "is the feed live right now?".

Streamlit UI (fallback):

```bash
streamlit run app.py
```

The ingestion lane (`backend/poller/`) has a test suite. Run it with a bare
`pytest` - `pytest.ini` supplies the paths, coverage and a 90 percent floor, so
a drop fails the run the same way a failing test does. It needs no network:
almost every test runs without a socket and the rest drive a stub on
127.0.0.1, so it never contacts MISO. CI runs it on every push.

```bash
pip install pytest pytest-cov   # dev tools, deliberately not in requirements.txt
pytest
```

Nothing else is covered yet. For the rest, minimum verification for any change:
the touched UI boots and a chat message round-trips (stub answer / graceful
handoff is fine). If you add backend code, also verify `uvicorn` boots and
`/ask` responds. The React
dev server proxies `/ask` to `localhost:8000` (vite.config.js) - keep that
contract: `POST /ask {question}` → `{answer, sources[{title,url}], as_of}`.
`answer` is markdown and may contain LaTeX (`$...$`/`$$...$$`), ` ```chart `
fenced JSON blocks (`{"type":"line|bar|area|pie","title","unit","labels",
"series":[{"name","data"}]}`, max 4 series) and ` ```map ` blocks
(`{"title","highlight":[state codes],"label"}`) - both UIs render all of it,
so keep both specs stable across prompt, React (`ChartBlock.jsx`,
`MapBlock.jsx`), and Streamlit (`render_answer` in app.py, which lists the
map's states as a caption instead of drawing). The map's state list is data
in `config.MISO_STATES`; the prompt may only highlight from it and the widget
ignores anything else. Frontend rule: React + plain CSS for
layout, no UI component libraries; approved rendering deps are listed in
frontend/UI_RULES.md §11.

## Architecture rules (do not "improve" these away)

The design is **pull-based RAG** - deliberate team decisions, not accidents:

1. **No live MISO API calls at question time.** A background poller (APScheduler
   inside FastAPI, every 5 min) fetches the four MISO endpoints and writes their
   JSON **verbatim** into `data/raw/`. The RAG lane reads those files and writes
   Chroma; question time only reads Chroma. This keeps the demo alive even if
   MISO's APIs go down. Answers may be about 10 min stale (the 5-min cadence plus
   MISO's own publication lag) - that's accepted and disclosed.
2. **Chroma is the ONLY store.** No SQLite, no extra databases. Question logging, if
   added, is a plain JSONL file.
3. **UPSERT, never append.** The RAG lane keeps one document per API endpoint with
   a **fixed doc_id**, overwritten each poll cycle. Appending snapshots makes vector
   search retrieve stale near-duplicates.
4. **JSON → prose before embedding.** This is the **RAG lane's** job, not the
   poller's: it reads `data/raw/*.json` and stores natural-language paragraphs
   ("As of 6:55 PM EST, total generation is 114,136 MW…"), never raw JSON. Raw
   numbers embed terribly.
5. **Timestamp in every snapshot**, and the system prompt must force "as of <time>"
   into answers - staleness stays visible, never hidden.
6. **Citations on every doc-lane answer.** The source URL is the product.
7. **RAG framework is LlamaIndex** (the Python library). Docs: load →
   `SentenceSplitter` chunks (~512 tokens, ~50 overlap) → embed → Chroma. Poller
   snapshots: the RAG lane builds a single small `Document` per endpoint from
   `data/raw/`, with a fixed `doc_id` and **no chunking**.
   Query: one search per lane over the same collection - top-2 snapshots plus
   top-4 document chunks, filtered by `doc_type` - so document chunks can never
   crowd the live numbers out of a question.
8. **Embeddings are local** (sentence-transformers all-MiniLM-L6-v2). LLM is Claude
   via the Anthropic API with a single tool, `search_docs(query)`.
9. **Out-of-scope questions get a graceful handoff** to MISO's contact form - never
   a hallucinated answer.
10. **The crosswalk is "AI drafts, human approves."** `build_crosswalk.py` has Claude
    propose report->API mappings, then drops any endpoint or field name that is not
    literally in the OpenAPI spec, then writes a *draft*. A human reads it and runs
    `--promote`. Never hand-edit `crosswalk.json` into something the spec cannot
    vouch for, and never let the prompt invent a field name - a wrong mapping sends
    a trader to the wrong column, which is worse than no crosswalk.

## Hard external constraints (from MISO mentors - violating these can get us banned)

- **Do NOT scrape miso.org.** It has anti-scraping protection; scrapers get
  IP-banned. Use the public APIs and politely-fetched documents only. The
  document corpus is nine hand-picked URLs in `backend/rag/doc_sources.json`,
  fetched once with a pause between requests - add to the list, never crawl.
- **Rate limit: max ~1 request per endpoint per minute** against
  `https://public-api.misoenergy.org` (free JSON, no auth). The 5-min poller is
  already far under this, and a per-link lease in
  `~/.cache/miso-copilot/rate-guard.json` enforces it before every request - keep
  both.
- API values come back as **strings**, not numbers - parse before doing math.
- MISO is mid-migration to the MISO Data Exchange API (pricing/load endpoints moving
  after Sept 30) - don't hard-fail if an endpoint disappears; degrade to the last
  stored snapshot.

## Secrets & hygiene

- The Anthropic API key lives in `.env` (gitignored). **Never commit keys**, never
  print them in logs or error messages.
- `data/` and `chroma_db/` are local stores - gitignored, never commit.
- `/ask` logs every request (ip, question, outcome, ms) to `data/logs/requests.jsonl`
  and rate-limits each IP to 20/min - see `backend/security.py`.

## Conventions

- Python, plain and readable - this is a hackathon repo judged on clarity of
  thought, not framework sophistication. Prefer a small readable function over an
  abstraction.
- Keep `requirements.txt` honest: dependencies stay commented out until the code
  that imports them lands.
- Comments are one-liners that say *why*, not essays. If a decision needs a
  paragraph, it goes in the module docstring or the folder README.
- `.gitignore` refuses any file named `* 2` / `* 2.*`: the repo lives in an
  iCloud-synced folder and iCloud drops such conflict copies, even inside `.git/`.
  If git errors oddly, look for them.
- Docstrings state what a module does and what's stubbed/pending (see `app.py`).
- If you change the architecture picture, update `docs/architecture.svg` (the
  simple one the README embeds) and `docs/architecture-detailed.svg` (plus its
  PNG render) - both are hand-edited SVG.
- Tests live in `tests/`, named for the behavior they protect rather than the
  function they call. Where a test exists because of a bug that actually
  happened, say so in a line above it - that is what stops someone deleting it
  as redundant later.
- `pytest` and `pytest-cov` stay out of `requirements.txt`. They are developer
  tools; nothing the product imports needs them.

## Definition of done for a change

1. App still boots (`streamlit run app.py`).
2. `pytest` passes, if you touched `backend/poller/`. A bare `pytest` is the
   whole command; it also enforces the coverage floor, so new code without
   tests fails the run rather than quietly lowering the number.
3. No secrets, no `data/` artifacts, no `__pycache__` in the commit.
4. README/docs updated if behavior or layout changed. This one has bitten the
   project twice: a README that still described a rate-guard rule the code had
   removed, and this file claiming there was no test suite after one landed. A
   document that contradicts the code is worse than no document, because it is
   believed.
5. Any new answer path includes a source URL and an "as of <time>" stamp.
