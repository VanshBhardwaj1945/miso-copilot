# backend/poller

Background poller for the API ingestion lane. It fetches MISO's live JSON
endpoints on a schedule and writes them to disk **unmodified**. That is the
whole job.

Four endpoints or five, depending on configuration. The four legacy public
display feeds - FuelMix, RealTimeTotalLoad, Snapshot, WindSolar - are always
polled. The MISO Data Exchange fuel-type feed joins them when `MISO_API_KEY`
is set; with no key it registers nothing and the four carry on, which is a
normal state rather than an error. `active_endpoints()` in `core.py` is that
decision, and it is the only place that makes it.

The Data Exchange feed differs from the four in three ways: a different host,
a subscription key sent as an `Ocp-Apim-Subscription-Key` header, and a paged
response that is assembled into one payload before it is written. The `{date}`
in its path is filled per cycle in fixed EST, matching MISO's own market-day
convention rather than local time.

It does not summarize, embed, chunk, or write to Chroma. The RAG lane
(`../rag/`, a separate workstream) reads the files this writes and does all
of that. The seam between the two lanes is the filesystem, not a function
call - either side can be rewritten, or run, without the other.

One exception, and it is wiring rather than fetching: `schedule.run_cycle()`
asks the RAG lane to re-read `data/raw/` once a cycle has finished, so Chroma
follows the poller instead of freezing at boot-time data. It passes the
directory, never the payloads, and the import is inside the function -
`core.py` still knows nothing about Chroma, and `python -m backend.poller`
still runs without the RAG dependencies installed.

## What it writes

Into `data/raw/` (gitignored, never committed):

```
FuelMix.json            verbatim response body
RealTimeTotalLoad.json  verbatim response body
Snapshot.json           verbatim response body
WindSolar.json          verbatim response body
DEFuelMix.json          every page concatenated, only when MISO_API_KEY is set
_status.json            sidecar: per-endpoint freshness and health
_status.lock            empty lock file, held while _status.json is written
*.tmp                   in-flight atomic writes, swept after 10 minutes
```

`DEFuelMix.json` is the one payload that is not byte-for-byte what a response
carried, because no single response carried it: the pages' `data` arrays are
concatenated and the last page's `page` block kept. Nothing else is touched. A
fetch that could not be completed - a page that failed, or paging metadata that
never said `lastPage` - is recorded as a failure and writes nothing, rather
than leaving a partial day on disk that reads as a whole one.

The last two are bookkeeping, not data. Anything scanning this directory
should skip `_status.lock` and `*.tmp`: the lock file is created by every
cycle and never has contents, and a `.tmp` file is a payload that is still
being written.

Payload files are overwritten in place, one per endpoint, so `data/raw/`
always holds exactly one fetch. `_status.json` carries `last_success`,
`consecutive_failures`, `last_error`, and `ref_id_changed_at` per endpoint,
plus the base URL and raw directory actually used. `ref_id_changed_at` is
how a frozen feed stays visible: MISO can serve stale data successfully,
and `last_success` alone will not show it.

`_status.json` is what the RAG lane should read before answering (it does
not yet; a per-endpoint health strip is on the to-do list). Note that
`cycle_finished_at` describes the cycle, not the data - a fresh cycle can
sit beside a half-hour-old `last_success`.

## Cadence and the rate guard

One cycle every **5 minutes** (`MISO_POLL_SECONDS`, default 300), matching
MISO's own publication rate. Worst-case answer staleness is about 10 min:
the cadence plus MISO's publication lag.

MISO asks that these links not be hit more than once per minute, and
mentors were explicit that abuse gets an IP banned. So the cadence is not
the only protection. Before **each endpoint**, the poller claims a per-link
lease under a file lock and writes the claim before issuing the request. A
link attempted less than 60 seconds ago is skipped for this cycle and logged.

One lease covers one endpoint's whole cycle, not one request. For the four
legacy feeds those are the same thing - a cycle is one request each. For the
paged Data Exchange feed they are not: a cycle there is up to five requests to
the same link, so the lease is claimed once for the endpoint and
`DE_PAGE_PAUSE_SECONDS` in `core.py` paces the pages inside it. That pause is
the guard's own 60 seconds, and for the same reason the guard's is: a shorter
one would breach the published limit from inside the lease, where nothing else
is watching.

The page size is deliberately large, so one page is the normal case and no
pause happens at all. The worst case is five pages (`DE_MAX_PAGES`), so four
pauses - 240 s, inside the 300 s cadence. A cycle that did overrun is skipped
rather than stacked, since the scheduled job is `coalesce=True,
max_instances=1`. Worth knowing before running `--once` by hand against a
multi-page day: it can sit for four minutes looking hung, and it is not.

The lease file lives at `~/.cache/miso-copilot/rate-guard.json`, outside
`data/` and outside the repo, so that no test configuration, no
`MISO_RAW_DIR` override, and no `rm -rf data/` can turn the rate limit off.
Writing the claim before the fetch is what makes a mid-cycle crash, a
restart, and `uvicorn --reload` safe. If the lease file cannot be read or
written, the cycle is skipped. A poller that cannot prove it is under the
limit does not fetch.

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
.venv/bin/python -m backend.poller --once     # one cycle, exit (the default)
.venv/bin/python -m backend.poller --loop     # one cycle now, then every cycle
.venv/bin/python -m backend.poller --status   # print freshness, exit, no network
```

`.venv/bin/python` rather than `python`, because macOS ships `python3` only
and a bare `python` exists just inside an activated virtualenv.

`--once` exits 0 if any endpoint succeeded, 2 if every endpoint was skipped
by the rate guard, and 1 if everything was attempted and nothing succeeded.

`--status` answers "is this live right now?" in one command, reading only
`_status.json`. It is the thing to run on demo day instead of reading raw
JSON in front of an audience.

## Environment

- `MISO_API_BASE` - default `https://public-api.misoenergy.org`. The rate
  guard is bypassed **only when the host is loopback**: `localhost`, or an
  IP address in a loopback range such as `127.0.0.1` or `::1`. Every other
  value stays guarded, however it is spelled - a stub on another machine,
  another laptop's hostname, a proxy, a typo. The warning printed every
  cycle says which of the two you have: `loopback, RATE GUARD BYPASSED` or
  `not loopback, rate guard ACTIVE`. This one value decides for the whole
  cycle, Data Exchange included, so a stub reached through
  `MISO_DATA_EXCHANGE_BASE` alone stays guarded - which is the safe way round.
- `MISO_RAW_DIR` - default `data/raw` inside the repo. Warns every cycle
  and is recorded in `_status.json`.
- `MISO_POLL_SECONDS` - default `300`, clamped to 5-3600. Below 60 only
  bites against a loopback stub, since the guard holds everything else to
  one request per link per minute.
- `MISO_POLLER_ENABLED` - default `1`. `0`, `false`, `no`, `off`, or empty
  disables the in-process scheduler only; the standalone commands above
  still run.
- `MISO_API_KEY` - unset by default, and unset means the four legacy feeds
  only. Set it and the Data Exchange feed is polled too, with the key sent as
  `Ocp-Apim-Subscription-Key`. Read from the environment alone, never from
  `config` - a key `.env` had loaded once outlived the environment clearing
  it. Never logged: the failure log line carries the URL, and the key is a
  header.
- `MISO_DATA_EXCHANGE_BASE` - the Data Exchange host, default
  `https://apim.misoenergy.org`. Separate from `MISO_API_BASE` because the two
  APIs are two hosts; point both at the stub to exercise all five links
  locally.

All of these are read after `.env` loading, so they can be set in `.env`. One
more, read by the API rather than the poller: `MISO_TRUST_PROXY=1` makes the
per-IP rate limiter honor `X-Forwarded-For` - set it only behind a real
reverse proxy, since anyone can send that header.

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
