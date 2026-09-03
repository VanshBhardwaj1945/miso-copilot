# backend/poller

Background poller for the API ingestion lane. It fetches four MISO public
JSON endpoints on a schedule and writes them to disk **unmodified**. That is
the whole job.

It does not summarize, embed, chunk, or write to Chroma. The RAG lane
(`../rag/`, a separate workstream) reads the files this writes and does all
of that. The seam between the two lanes is the filesystem, not a function
call - either side can be rewritten, or run, without the other.

Full detail lives in the API ingestion specification; this README is the
short version.

## What it writes

Into `data/raw/` (gitignored, never committed):

```
FuelMix.json            verbatim response body
RealTimeTotalLoad.json  verbatim response body
Snapshot.json           verbatim response body
WindSolar.json          verbatim response body
_status.json            sidecar: per-endpoint freshness and health
```

Payload files are overwritten in place, one per endpoint, so `data/raw/`
always holds exactly one fetch. `_status.json` carries `last_success`,
`consecutive_failures`, `last_error`, and `ref_id_changed_at` per endpoint,
plus the base URL and raw directory actually used. `ref_id_changed_at` is
how a frozen feed stays visible: MISO can serve stale data successfully,
and `last_success` alone will not show it.

`_status.json` is what the RAG lane should read before answering. Note that
`cycle_finished_at` describes the cycle, not the data - a fresh cycle can
sit beside a half-hour-old `last_success`.

## Cadence and the rate guard

One cycle every **5 minutes** (`MISO_POLL_SECONDS`, default 300), matching
MISO's own publication rate. Worst-case answer staleness is about 10 min:
the cadence plus MISO's publication lag.

MISO asks that these links not be hit more than once per minute, and
mentors were explicit that abuse gets an IP banned. So the cadence is not
the only protection. Before **each individual request**, the poller claims
a per-link lease under a file lock and writes the claim before issuing the
request. A link attempted less than 60 seconds ago is skipped for this
cycle and logged.

The lease file lives at `~/.cache/miso-copilot/rate-guard.json`, outside
`data/` and outside the repo, on purpose: no test configuration, no
`MISO_RAW_DIR` override, and no `rm -rf data/` can turn the rate limit off.
Writing the claim before the fetch is also on purpose - it is what makes a
mid-cycle crash, a restart, and `uvicorn --reload` safe. If the lease file
cannot be read or written, the cycle is skipped. A poller that cannot prove
it is under the limit does not fetch.

Two operational rules that nothing enforces: run the API with a **single
worker** (under `--workers N` the scheduler starts in every one), and run
the poller on **one machine at a time** (the lease is a local file and
cannot see another laptop).

## Running it

Inside the API, an APScheduler job runs one cycle at startup and one every
`MISO_POLL_SECONDS` after that:

```bash
uvicorn backend.main:app --reload --port 8000
```

Standalone:

```bash
python -m backend.poller --once     # one cycle, exit (the default)
python -m backend.poller --loop     # one cycle now, then every cycle
python -m backend.poller --status   # print freshness, exit, no network
```

`--once` exits 0 if any endpoint succeeded, 2 if every endpoint was skipped
by the rate guard, and 1 if everything was attempted and nothing succeeded.

`--status` answers "is this live right now?" in one command, reading only
`_status.json`. It is the thing to run on demo day instead of reading raw
JSON in front of an audience.

## Environment

- `MISO_API_BASE` - default `https://public-api.misoenergy.org`. A
  non-default value means a local stub, so the rate guard is bypassed.
  Warns every cycle.
- `MISO_RAW_DIR` - default `data/raw` inside the repo. Warns every cycle
  and is recorded in `_status.json`.
- `MISO_POLL_SECONDS` - default `300`, clamped to 5-3600. Below 60 only
  bites against a stub, since the guard holds the real feed to one request
  per link per minute.
- `MISO_POLLER_ENABLED` - default `1`. `0`, `false`, `no`, `off`, or empty
  disables the in-process scheduler only; `python -m backend.poller` still
  runs.

All four are read after `.env` loading, so they can be set in `.env`.

Both path overrides are demo-day hazards: export one for a test, start the
demo server from that shell an hour later, and the poller quietly does the
wrong thing while the status file looks healthy. Hence the warning every
cycle and the recorded values in `_status.json`.

## Demo fallback

A known-good copy of `data/raw/` is kept at `data/raw.backup/`. It is
inside the gitignored `data/` tree and is never committed. If a cycle
poisons `data/raw/` on demo day, restore it with:

```bash
cp data/raw.backup/* data/raw/
```

The backup includes `_status.json`, so `--status` will correctly report the
restored data as stale. That is the honest answer, and staleness is
disclosed in every answer anyway.
