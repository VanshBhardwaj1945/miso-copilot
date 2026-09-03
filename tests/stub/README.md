# The MISO stub server

A local stand-in for the MISO public API, so the acceptance criteria in the
API ingestion specification can be run without touching MISO. It serves the
four polled links from synthetic fixtures, counts requests per link, and can
be told to fail in the exact ways the criteria describe.

It binds to `127.0.0.1` and makes no outbound requests. Nothing under
`backend/` imports it.

## Fixtures are synthetic, and no MISO data is ever committed

`fixtures/*.json` are hand-written minimal payloads. They are not captures.
They mimic the structure recorded in `miso-api-field-notes.md` - all values
strings or null, the same nesting, one WindSolar row with null actuals - but
the numbers are obviously fake and every timestamp is January 1, 1970.

Minimal means minimal: two fuel categories instead of eight, two or three
load readings instead of 24 and 252, two Snapshot rows instead of four, two
WindSolar rows instead of 48.

They are committed on purpose, because a stub that cannot be reproduced from
a checkout is not a test. Real captured payloads belong in `data/raw/`, which
is gitignored and never committed. Do not replace these fixtures with
captured data.

## Starting it

From the repo root:

    python -m tests.stub.server --port 8971
    python -m tests.stub.server --port 8971 --mode html
    python -m tests.stub.server --port 0 --mode fail-one \
        --fail-endpoint WindSolar

`--port 0` asks the OS for a free port. The chosen port is printed on
startup either way. Point the poller at it with
`MISO_API_BASE=http://127.0.0.1:8971`, and send its output somewhere
harmless with `MISO_RAW_DIR`. A non-default base bypasses the rate guard, so
no 60-second wait applies.

From a test, run it on a thread instead:

    from tests.stub.server import serve_in_thread

    httpd, thread = serve_in_thread(modes=["redirect"])
    base = f"http://127.0.0.1:{httpd.server_port}"
    ...
    httpd.shutdown()
    httpd.server_close()

## Paths

| Path | Serves |
|---|---|
| `/api/FuelMix` | `fixtures/FuelMix.json` |
| `/api/RealTimeTotalLoad` | `fixtures/RealTimeTotalLoad.json` |
| `/api/Snapshot` | `fixtures/Snapshot.json` |
| `/api/WindSolar/GetCombined` | `fixtures/WindSolar.json` |
| `/_counts` | per-link request counts and current mode, JSON |
| `/_mode?mode=html` | switch modes without restarting |
| `/_reset` | zero the counters |
| `/_health` | liveness |

Counts are per link, keyed by path, because criteria 8 and 10 ask how many
times one link was hit, not how many requests the stub saw. A request is
counted before the response is chosen, so a 302 or a 503 counts exactly like
a served payload. Control endpoints are never counted.

## Failure modes

Pass one with `--mode`, or several comma separated
(`--mode empty-load,empty-snapshot`), or switch at runtime through `/_mode`.

| Mode | Behavior | What the poller records |
|---|---|---|
| `ok` | valid fixtures, `RefId` advances on every serve | success |
| `frozen` | valid fixtures, `RefId` never changes | `ref_id_changed_at` stalls |
| `html` | 200 with an HTML body | `invalid JSON` |
| `redirect` | 302 with a valid `Location` | `redirect 302`, one request |
| `empty-load` | `{"LoadInfo": {}}` for RealTimeTotalLoad | `missing ref_id` |
| `empty-snapshot` | `[{}]` for Snapshot | `shape check failed` |
| `fail-one` | 503 on `--fail-endpoint`, three served | `HTTP 503` on one link |
| `fail-all` | 503 on every link | `HTTP 503` on all four |

Precedence runs broadest first: `fail-all`, then `fail-one`, then `html`,
then `redirect`, then the empty payloads.

`ok` mode rewrites the `RefId` to `... rev 1`, `rev 2` and so on as it
serves, so a running poller sees a live feed. `frozen` leaves the fixture
value alone, which is the whole point of criterion 14: `last_success`
advances while `ref_id_changed_at` does not. Use `--no-rotate` to serve the
fixture bytes unmodified in `ok` mode.

## Which criteria use it

Ten of the eighteen acceptance criteria in section 9 of the specification:

| Criterion | Mode |
|---|---|
| 5, network failure | none - point at a closed port, not at the stub |
| 6, bad payload | `html` |
| 7, empty but shaped | `empty-load,empty-snapshot` |
| 8, redirects not followed | `redirect`, then check `/_counts` reads 1 |
| 9, partial failure isolated | `fail-one` |
| 10, rate discipline | `ok`, `MISO_POLL_SECONDS=10`, check `/_counts` |
| 13, status merge across restart | `fail-all` |
| 14, frozen feed visible | `frozen`, `MISO_POLL_SECONDS=10` |
| 15, `--status` | any - it makes no request |
| 17, hygiene | any |

Criteria 1 to 4, 11 and 12 run against live MISO and the real rate guard, so
the stub takes no part in them.
