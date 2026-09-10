# Spec: bring the MISO Data Exchange APIs into MISO Ramen

**Status:** decisions made 2026-09-10, ready to build. Nothing here is built yet.
**Written:** 2026-09-10
**Companion:** [`miso-data-exchange-apis.md`](miso-data-exchange-apis.md) - the endpoint reference this is based on.

---

## Why this matters now

MISO retires its CSV market reports on **Sept 30** - twenty days out. The
Data Exchange API is the replacement. Today the product touches it in exactly
one place: the crosswalk tells people where a retired report's columns went.
It has never called the API.

Three separate things are on the table, and they are worth keeping apart
because they have different value and different cost:

| | What it buys | Cost |
|---|---|---|
| **A. Crosswalk validation** | `build_crosswalk.py` stops validating against a reconstructed spec | ~1 hour, mostly signing up |
| **B. Continuity** | Live answers survive Sept 30, when the legacy feeds may go | 1-2 days |
| **C. Regional answers** | "How much wind in MISO South right now?" - not answerable today | 1-2 days on top of B |

All three are in scope (decision 1). **A goes first regardless** - it is nearly
free, it improves a feature that already ships, and it answers the two open
questions below that B and C depend on.

---

## What we verified

Read from the portal's published definitions on 2026-09-10. **No live calls** -
we have no subscription key, so the API returns
`401 Access denied due to missing subscription key`.

- Two APIs: `https://apim.misoenergy.org/lgi` and `.../pricing`, **32 operations**.
- **12 accept `region`**, all in LGI, all with the same enum:
  `NORTH | CENTRAL | SOUTH | MISO | NO_REGION`. No default.
- Every one of those 12 carries `region` **on each output row**, so one
  unfiltered call returns all regions and you group client-side.
- `/v1/real-time/{date}/demand/actual` alone has `geoResolution`
  (`region` | `localResourceZone`, default `region`). Rows carry one **or** the
  other, never both.
- The Pricing API has **no region at all** - it is `node` (LMP) and `zone`
  (ASM reserve zones), which are not North/Central/South.
- **Everything is paged**: `pageNumber`, `pageSize`, and a `page` block with
  `lastPage`.
- Every path is date-scoped: `/v1/real-time/{date}/...`.

### Two things the definitions do not prove

1. **Does omitting `region` really return every region?** No default is
   documented, which conventionally means unfiltered - but the examples show a
   single row each.
2. **Do `MISO` and `NO_REGION` rows come back alongside the three regions?**
   If a footprint-wide `MISO` row arrives with them, naive summing
   double-counts.

Both resolve in one call once we have a key. **Answer them before building B or
C**: the second one decides whether the prose sums three regions or four, and
getting that wrong double-counts the footprint total in every answer.

---

## Decisions (settled)

Reviewed one at a time on 2026-09-10.

### 1. Scope: **A + B + C** - everything

Crosswalk validation, continuity past Sept 30, and regional answers. A first,
since B and C depend on the two open questions it answers.

### 2. Endpoints: **`/v1/real-time/{date}/generation/fuel-type` only, then expand**

One endpoint proves auth, `{date}`, paging and region together, with the least
rate-guard exposure. It covers today's fuel-mix answer and carries `region`, so
it delivers C on its own.

Consequence worth noting: **local-resource-zone granularity is not available**
from this endpoint. `geoResolution` exists only on `demand/actual`. LRZ answers
wait for the expansion.

### 3. Legacy feeds: **run both in parallel**

The four legacy feeds stay exactly as they are, with their own doc_ids. The new
feed earns trust beside them. Retire nothing until Sept 30 has passed and the
Data Exchange path has real hours behind it.

### 4. Storage: **one document per endpoint - identical to the current poller**

No new lane, no change to `LIVE_TOP_K`, no change to `retriever.py`. The new
endpoint is fetched, transformed and upserted exactly the way `FuelMix` is
today: one raw JSON file, one prose document, one fixed `doc_id`, overwritten
each cycle. Architecture rule 3, read literally.

This was considered against splitting into one document per region, which would
embed each region more sharply. **Deliberately rejected as out of scope** -
matching the established pattern matters more than speculative retrieval
tuning, and the existing lane was itself only added after crowding was actually
observed rather than predicted.

*Accepted risk:* all five regions live in one paragraph, so a question like
"wind in MISO South" depends on Claude reading the right region out of the
retrieved text rather than on the retriever isolating it. If regional answers
come back vague in practice, revisit this decision first - it is the most
likely cause, and splitting per region is a contained change.

### 5. Subscription key: **`MISO_API_KEY` in `.env`, same as `CLAUDE_API_KEY`**

Read through `backend/config.py`, sent as `Ocp-Apim-Subscription-Key`. Never
logged, never in instance metadata, never committed. Absent key means the new
endpoint registers nothing and logs one line - the app boots and the legacy
feed carries on, exactly as a missing Claude key degrades to the handoff.

---

## Proposed design

Nothing below changes the architecture rules. Still pull-based, still Chroma
only, still prose before embedding, still no live calls at question time.

```
Data Exchange  ──poller, every 5 min──▶ data/raw/de_*.json  (verbatim)
                                              │
                                   transformers.py  JSON → prose
                                              ▼
                                 one fixed doc_id per endpoint  (UPSERT)
                                              ▼
                                    the same Chroma collection
```

### Poller changes

`Endpoint` today is `(key, path, shape, ref_path)` and assumes an unauthenticated
GET with no query string. Data Exchange needs three more things: a header, a
date in the path, and paging. Extend rather than fork:

```python
class Endpoint(NamedTuple):
    key: str
    path: str                      # may contain {date}
    shape: Callable[[object], bool]
    ref_path: tuple[str, ...] | None
    base: str = MISO_PUBLIC_BASE    # which host
    auth: bool = False              # send Ocp-Apim-Subscription-Key
    paged: bool = False             # follow page.lastPage
```

The existing four keep working unchanged - every new field has a default.

- **`{date}`** is filled with today in **fixed EST**, matching the `as_of` rule
  in `routes/ask.py`. MISO's own convention, and it avoids a DST-shifted
  off-by-one-day at midnight.
- **Paging**: follow `page.lastPage`, concatenate `data`, and cap at a
  configured maximum of pages so a runaway response cannot fill the disk. Write
  the assembled result as one JSON file, so `data/raw/` stays one-file-per-endpoint.
- **The rate guard covers this unchanged.** It leases per link. A paged fetch is
  several requests to one link, so either lease per page or widen the lease to
  cover the whole cycle - **this needs a decision during implementation**, and
  it is the part most likely to breach MISO's 1-request-per-endpoint-per-minute
  rule if done carelessly.

### Prose

`transform_*` keeps its contract: `(data) -> (prose, as_of, source_url)` - the
same signature as the four existing transformers. One document covering every
region, naming each in turn:

> As of 10-Sep-2026 interval 09:25 EST, MISO **North** generated 31,204 MW:
> coal 11,908 MW (38.2%), natural gas 9,771 MW (31.3%), wind 4,102 MW (13.1%)...

`source_url` is the Data Exchange portal page for that operation, so rule 6
holds and the citation is a real link.

### Files touched

| File | Change |
|---|---|
| `backend/config.py` | `MISO_API_KEY`, Data Exchange base URL |
| `backend/poller/core.py` | `Endpoint` fields, auth header, `{date}`, paging |
| `backend/poller/guard.py` | decide paged-fetch leasing (see above) |
| `backend/rag/transformers.py` | `transform_de_fueltype`, region-aware |
| `backend/rag/ingest_api.py` | one new `ENDPOINTS_CONFIG` entry, one fixed doc id |
| ~~`backend/rag/retriever.py`~~ | **unchanged** - decision 4 |
| `backend/llm/prompts.py` | teach it regional answers exist |
| `tests/` | stub-server fixtures for auth, paging, region |
| `data/specs/` | the official OpenAPI YAML (item A) |

---

## Testing

The poller suite is 634 tests at 100% branch coverage and must stay there.
New ground the stub server has never covered:

- **auth** - key present, key absent, key rejected (401)
- **paging** - single page; several pages; `lastPage` never arriving (the cap);
  a page that returns no `data`
- **`{date}`** - the substitution, and the EST boundary at local midnight
- **regions** - all five values present; `MISO`/`NO_REGION` present or absent
  (whichever open question 2 turns out to be); a region missing entirely
- **degradation** - Data Exchange down, key missing, malformed payload: the
  legacy feed and the app must be unaffected

No test may contact MISO. Everything goes through the existing 127.0.0.1 stub.

---

## Phasing

| Phase | Work | Effort | Gate |
|---|---|---|---|
| **A** | Sign up, drop the OpenAPI in `data/specs/`, re-run `build_crosswalk.py --promote`, answer the two open questions | ~1 hour | this week |
| **B1** | `Endpoint` extension: auth, `{date}`, paging. `generation/fuel-type` only. Stub-server tests for all three | ~1 day | after demo |
| **B2** | `transform_de_fueltype`, one doc id, prompt learns regional answers exist | ~0.5 day | after B1 is stable |
| **C** | Verify regional answers actually work; if vague, revisit decision 4 | ~0.5 day | after B2 |
| **later** | `demand/actual` (adds LRZ), `fuel-on-the-margin` | ~1 day | only when wanted |

---

## Risks

**Rate limiting is the one that can get us banned.** MISO's published limit is
~1 request per endpoint per minute. A paged fetch is several requests to one
link, and the guard has never seen that shape. Get the leasing decision right
before the first live run, and keep the cadence at 5 minutes.

**Chroma single-writer.** Adding endpoints does not change that only one process
may write. This has already bitten us once in production.

**Scope creep into pricing.** The Pricing API has no region and is not part of
this. The crosswalk already covers what people ask about pricing.

**A key in a public repo.** `MISO_API_KEY` follows `CLAUDE_API_KEY` exactly:
`.env`, gitignored, mode 600. Note `.gitignore` was recently narrowed from
`data/` to `data/chroma` + `data/raw`, so `data/logs/` and `data/docs/` are now
tracked - worth re-checking before any new file lands under `data/`.

---

## Out of scope

- Live API calls at question time (architecture rule 1 - unchanged)
- Any datastore other than Chroma (rule 2)
- The Pricing API
- Historical/backfill queries. Every path is date-scoped, so this *could* answer
  "what was the fuel mix last Tuesday" - a genuinely new capability, and a
  different feature with a different design. Not here.
