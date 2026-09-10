# backend/rag

Chroma + LlamaIndex retrieval layer. Two feeds go in, one search comes out.

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
- `doc_sources.json` - the curated reference corpus: nine documents, each with
  the local filename, the misoenergy.org URL we cite, and (when different) the
  URL we download from. This list is the reproducible part; MISO's files are not.
- `fetch_docs.py` - one-time polite download of that list into `data/docs/`
  (gitignored): browser User-Agent, a pause between requests, no link-following.
  PDFs are checked for real `%PDF` bytes (a rotated CDN filename returns an
  AccessDenied XML with a 200). HTML pages - the Market Reports catalog and
  the generator-interconnection page - are converted to small markdown files,
  with each list item prefixed by its section heading so a chunk from the
  middle of a long list still says which category it belongs to.
- `ingest_docs.py` - chunks everything in `data/docs/` (SentenceSplitter,
  512 tokens, 50 overlap) into Chroma as `doc_type: reference_doc`, stamping
  each chunk with the title and citation URL from `doc_sources.json`. File
  metadata (paths, dates) is kept out of the embedding. Evicts the previous
  corpus first, so re-running never duplicates. Stop the backend before
  running it - two processes writing Chroma at once has corrupted it before.
- `crosswalk.json` - the report-to-API crosswalk: 18 entries, each mapping one
  retiring CSV market report (its columns, `HE 1..24`, `Value = MLC` row labels)
  to the Data Exchange endpoint, parameters and field names that replace it,
  plus a one-line note on the shape change. Ingested by `ingest_docs.py` as one
  prose paragraph per entry, cited to the report's readers' guide.
- `build_crosswalk.py` - drafts that file. Feeds each readers' guide plus the
  endpoint catalog from `data/specs/` to Claude (structured output, so the reply
  is always valid JSON), then a validator drops any endpoint or field not
  literally in the spec. Writes `crosswalk.draft.json` (gitignored) for a human
  to read; `--promote` turns it into `crosswalk.json`. Parameter *values*
  (`preliminaryFinal=Final`, `timeResolution=hourly`) are not in the spec and
  were checked by hand against gridstatus, an open-source client.
- `site_index.md` - a map of which misoenergy.org page covers which topic:
  209 pages, each as `name - where it sits - URL`. The most common question is
  "where do I find X?", and for that the link is the answer; without this the
  model declines rather than guess a URL. Ingested by `ingest_docs.py` as
  ordinary reference-doc chunks. It cites MISO's home page, but the real page
  URLs are in the chunk text, which is what lets an answer link the exact page.
  Tracked, like `crosswalk.json`, because it is generated rather than fetched -
  there is no per-file URL for `fetch_docs` to pull.
- `build_site_index.py` - regenerates that file. One request for MISO's
  published `sitemap.xml`, then a filter: events and stakeholder engagement are
  86 percent of the 2,595 URLs and never the answer to "where do I find X?", so
  they go, along with news releases and dated notification posts. That is a
  sitemap fetch, not a crawl - the URL list it returns is **not** a license to
  fetch those pages.
- `transformers.py` also carries `transform_de_fueltype`, for the MISO Data
  Exchange feed. Same contract as the four legacy transformers - JSON in, one
  paragraph out - but the rows are per region, so the paragraph names MISO
  North, Central and South in turn. It says explicitly that "MISO overall" is
  the footprint total and must not be added to the three, because that
  double-count is the easy mistake for a reader and for the model.
- `retriever.py` - `search_docs(query)`: searches each lane separately -
  top-2 live snapshots and top-4 document chunks, by `doc_type` filter - and
  hands both to Claude, snapshots first. One shared top-k let Fact Sheet
  chunks crowd the live numbers out of "what are grid conditions?" questions;
  a seat per lane is the fix. Returns context + source links + freshest "as of".

Building the corpus (once, or whenever `doc_sources.json` changes):

```bash
.venv/bin/python -m backend.rag.fetch_docs     # downloads 9 files, ~30 s
.venv/bin/python -m backend.rag.ingest_docs    # docs + crosswalk + site index; backend stopped first
```

Regenerating the crosswalk (after adding guides, or with the official spec):

```bash
.venv/bin/python -m backend.rag.build_crosswalk            # ~1 min, three Claude calls
# read backend/rag/crosswalk.draft.json
.venv/bin/python -m backend.rag.build_crosswalk --promote
.venv/bin/python -m backend.rag.ingest_docs
```

Chroma follows the poller: `schedule.run_cycle()` polls and then calls
`sync_raw_snapshots()`, so answers no longer freeze at boot-time data. The
re-sync runs even when the poll failed - `data/raw/` still holds the last
good files and the sync is idempotent, so a Chroma that started empty fills
itself on the next cycle. Documents are refreshed by re-running the two
commands above; they change monthly to yearly, not every five minutes.
