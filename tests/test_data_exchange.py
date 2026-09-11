"""The Data Exchange endpoint: registration, auth, {date}, and paging.

No network. Paging is driven by stubbing core._fetch, so these never open a
socket - not even to the 127.0.0.1 stub.

The Data Exchange feed runs *beside* the four legacy display feeds rather than
replacing them, so most of what is protected here is the boundary: with no
subscription key nothing about the old behavior may change.
"""

import json

import pytest

from backend.poller import core, guard

# Captured at import, before no_page_pause below can patch it to zero: the two
# tests that pin the pacing decision need the real constant, not the value the
# rest of the suite runs with.
REAL_PAGE_PAUSE_SECONDS = core.DE_PAGE_PAUSE_SECONDS


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("MISO_API_KEY", raising=False)
    monkeypatch.setattr(core.config, "MISO_API_KEY", None)


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("MISO_API_KEY", "sub-key-123")
    return "sub-key-123"


def page(rows, last=True):
    """One Data Exchange page as the API documents it."""
    return {"data": rows, "page": {"lastPage": last, "pageNumber": 1}}


def ok(body):
    """A successful core._fetch result carrying `body`."""
    content = json.dumps(body).encode()
    return {"ok": True, "error": None, "http_status": 200,
            "bytes": len(content), "ref_id": None, "content": content}


ROWS = [{"region": "North", "fuel": "Wind", "mw": 4102.0}]


# --- registration --------------------------------------------------------

def test_no_key_means_the_legacy_four_and_nothing_else(no_key):
    """A missing key is a normal state, not an error - the demo path is the four."""
    assert [e.key for e in core.active_endpoints()] == [e.key for e in core.ENDPOINTS]


def test_a_key_adds_the_data_exchange_endpoint(with_key):
    keys = [e.key for e in core.active_endpoints()]
    assert keys[:4] == [e.key for e in core.ENDPOINTS]
    assert "DEFuelMix" in keys


def test_the_legacy_endpoints_are_untouched_by_the_new_fields():
    """Every added Endpoint field defaults to legacy behavior."""
    for endpoint in core.ENDPOINTS:
        assert endpoint.source == core.PUBLIC
        assert endpoint.paged is False


# --- key plumbing --------------------------------------------------------

def test_the_key_is_sent_only_to_data_exchange(with_key):
    de = core.DATA_EXCHANGE_ENDPOINTS[0]
    assert core._headers_for(de)["Ocp-Apim-Subscription-Key"] == with_key
    assert "Ocp-Apim-Subscription-Key" not in core._headers_for(core.ENDPOINTS[0])


def test_no_key_sends_no_key_header(no_key):
    """Belt and braces: active_endpoints already withholds the endpoint."""
    de = core.DATA_EXCHANGE_ENDPOINTS[0]
    assert "Ocp-Apim-Subscription-Key" not in core._headers_for(de)


def test_the_key_comes_from_the_environment_only(monkeypatch):
    """config.MISO_API_KEY is captured at import; reading it too would
    resurrect a key the environment has cleared, which is how a configured
    .env silently broke eight unrelated tests."""
    monkeypatch.setattr(core.config, "MISO_API_KEY", "stale-import-time-value")
    monkeypatch.delenv("MISO_API_KEY", raising=False)
    assert core.data_exchange_key() is None
    monkeypatch.setenv("MISO_API_KEY", "live")
    assert core.data_exchange_key() == "live"


# --- base URL and {date} -------------------------------------------------

def test_data_exchange_uses_its_own_host(no_key):
    de = core.DATA_EXCHANGE_ENDPOINTS[0]
    assert core.endpoint_base(de, "https://public-api.example") != "https://public-api.example"
    assert core.endpoint_base(core.ENDPOINTS[0], "https://public-api.example") \
        == "https://public-api.example"


def test_the_data_exchange_host_is_overridable(monkeypatch):
    """So the stub server can stand in for MISO."""
    monkeypatch.setenv("MISO_DATA_EXCHANGE_BASE", "http://127.0.0.1:8971")
    de = core.DATA_EXCHANGE_ENDPOINTS[0]
    assert core.endpoint_base(de, "https://ignored") == "http://127.0.0.1:8971"


def test_date_is_substituted_in_fixed_est(monkeypatch):
    """MISO stamps market days in EST all year. A DST-aware zone asks for the
    wrong day for an hour each night - the same trap the as-of stamp avoids.

    The date is the newest *published* market day, not today; see
    test_the_date_asked_for_is_never_today.
    """
    de = core.DATA_EXCHANGE_ENDPOINTS[0]
    url = core.endpoint_url(de, "https://x")
    assert "{date}" not in url
    assert core.market_date() in url


# --- market date ---------------------------------------------------------

def test_the_date_asked_for_is_never_today():
    """MISO answers today with 400 `data not available yet for this market
    date`: these endpoints publish a completed day at 2am EST the day after,
    whatever the /real-time/ path suggests. Asking for today failed every
    cycle until this was found against the live API.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("EST")).strftime("%Y-%m-%d")
    assert core.market_date() != today
    assert today not in core.endpoint_url(core.DATA_EXCHANGE_ENDPOINTS[0], "https://x")


@pytest.mark.parametrize("hour,expected", [
    (0, "2026-09-08"), (1, "2026-09-08"),      # before 2am: yesterday is not published
    (2, "2026-09-09"), (3, "2026-09-09"),      # from 2am: yesterday is available
    (23, "2026-09-09"),
])
def test_the_publish_boundary_is_two_am_est(hour, expected):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    when = datetime(2026, 9, 10, hour, 30, tzinfo=ZoneInfo("EST"))
    assert core.market_date(when) == expected


def test_a_clock_in_another_zone_is_converted_not_assumed():
    """06:00 UTC is 01:00 EST - before the publish hour, so two days back.

    The hour matters. This test used to pass 01:30 UTC, which lands in the
    five-hour band where the converted and unconverted answers coincide: it
    went green with the conversion deleted. Only 02:00-06:59 UTC tells the
    two apart.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    when = datetime(2026, 9, 10, 6, 0, tzinfo=ZoneInfo("UTC"))
    assert core.market_date(when) == "2026-09-08"


def test_a_naive_clock_is_read_as_est_not_as_the_machines_local_time():
    """astimezone reads a naive value as the host's zone, so the same call
    returned different days on a laptop in EDT and a server in UTC - and the
    difference lands exactly on the 2am boundary this function exists for."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    naive = datetime(2026, 9, 10, 3, 0)
    aware = datetime(2026, 9, 10, 3, 0, tzinfo=ZoneInfo("EST"))
    assert core.market_date(naive) == core.market_date(aware) == "2026-09-09"


@pytest.mark.parametrize("hour,minute,expected", [
    (2, 0, "2026-09-08"),      # 2am sharp: MISO may not have finished
    (2, 14, "2026-09-08"),
    (2, 15, "2026-09-09"),     # past the grace margin, yesterday is available
    (2, 16, "2026-09-09"),
])
def test_the_publish_hour_carries_a_grace_margin(hour, minute, expected):
    """MISO's "2am" is not to the second. Without a margin a publish that
    slips by a minute made every cycle in that window take a 400, which reads
    as an outage rather than as being early."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    when = datetime(2026, 9, 10, hour, minute, tzinfo=ZoneInfo("EST"))
    assert core.market_date(when) == expected


def test_the_registered_endpoints_are_pinned_by_name():
    """Deleting a feed is a test failure, not a quieter cycle.

    Every other assertion about this table iterates it, so it is vacuously
    true over a shorter list: ten of the eleven could be removed - reverting
    the feature - with the whole suite still green.
    """
    assert {e.key for e in core.DATA_EXCHANGE_ENDPOINTS} == {
        "DEFuelMix", "DEActualLoad", "DEFuelOnMargin", "DEDayAheadDemand",
        "DEDayAheadFuelMix", "DEClearedPhysical", "DEClearedVirtual",
        "DEOfferedEcoMax", "DEOfferedEcoMin", "DENetScheduled", "DELoadForecast",
    }


# --- the paging pause budget ---------------------------------------------

def test_a_pause_is_taken_from_the_cycles_budget(monkeypatch):
    slept = []
    monkeypatch.setattr(core.time, "sleep", slept.append)
    budget = core.PagePauseBudget(100)
    assert budget.spend(60) is True
    assert budget.remaining == 40
    assert slept == [60]


def test_a_cycle_stops_pausing_once_its_budget_is_spent(monkeypatch):
    """The pauses are sequential and block every endpoint behind them. Twelve
    paged endpoints pausing freely would sleep 44 minutes against a 300-second
    cadence, and the live display feeds - the product - would stop refreshing
    for all of it."""
    slept = []
    monkeypatch.setattr(core.time, "sleep", slept.append)
    budget = core.PagePauseBudget(60)
    assert budget.spend(60) is True
    assert budget.spend(60) is False     # and does not sleep anyway
    assert slept == [60]


def test_an_endpoint_that_runs_out_of_budget_fails_rather_than_hurrying(monkeypatch):
    """Fetching the next page early would breach the rate limit the pause
    exists to keep, and the penalty is an IP ban we cannot undo. A loud
    failure retries next cycle; a hurried fetch cannot be undone."""
    calls = []

    def fake_fetch(endpoint, url):
        calls.append(url)
        body = {"data": [{"region": "NORTH"}],
                "page": {"lastPage": False, "pageNumber": len(calls)}}
        return {"ok": True, "content": json.dumps(body).encode(),
                "status": 200, "bytes": 1, "error": None}

    monkeypatch.setattr(core, "_fetch", fake_fetch)
    monkeypatch.setattr(core, "DE_PAGE_PAUSE_SECONDS", 60)
    endpoint = core.DATA_EXCHANGE_ENDPOINTS[0]
    result = core._fetch_all_pages(endpoint, "https://x/y", core.PagePauseBudget(0))
    assert result["ok"] is False
    assert "budget" in result["error"]
    assert len(calls) == 1               # page two was never requested


def test_a_path_without_a_date_is_left_alone():
    assert core.endpoint_url(core.ENDPOINTS[0], "https://x") == "https://x/api/FuelMix"


# --- shape gate ----------------------------------------------------------

@pytest.mark.parametrize("body", [
    None, [], "text", {}, {"data": "not a list"}, {"data": []},
    {"data": [{"fuel": "Wind"}]},          # row with no region
    {"data": [{"region": "North"}, {"fuel": "x"}]},   # one bad row spoils it
])
def test_shape_gate_rejects_anything_without_regions(body):
    """The point of this endpoint is region. A payload without it is not it."""
    assert core._shape_de_regional(body) is False


def test_one_gate_serves_every_region_endpoint():
    """The twelve operations differ in their value fields - load, nsi, supply,
    mustRun - but share the {data, page} envelope and a region on every row."""
    for values in ({"load": 1.0}, {"nsi": -2.0}, {"supply": 3.0},
                   {"mustRun": 1, "economic": 2, "emergency": 3},
                   {"fuelTypes": {"wind": 5.0}, "totalMw": 5.0}):
        body = {"data": [{"region": "NORTH", **values}], "page": {"lastPage": True}}
        assert core._shape_de_regional(body) is True


def test_every_registered_endpoint_has_a_transformer():
    """A polled endpoint with no ingest entry writes a file nothing reads."""
    from backend.rag.ingest_api import ENDPOINTS_CONFIG
    for endpoint in core.DATA_EXCHANGE_ENDPOINTS:
        assert f"{endpoint.key}.json" in ENDPOINTS_CONFIG, endpoint.key


def test_every_registered_endpoint_is_region_scoped_and_dated():
    for endpoint in core.DATA_EXCHANGE_ENDPOINTS:
        assert "{date}" in endpoint.path, endpoint.key
        assert endpoint.path.startswith("/lgi/"), endpoint.key
        assert endpoint.paged is True, endpoint.key


def test_shape_gate_accepts_a_real_page():
    assert core._shape_de_regional(page(ROWS)) is True


# --- paging --------------------------------------------------------------

def test_a_single_page_is_returned_whole(monkeypatch):
    monkeypatch.setattr(core, "_fetch", lambda e, u: ok(page(ROWS)))
    de = core.DATA_EXCHANGE_ENDPOINTS[0]
    result = core._fetch_all_pages(de, "https://x/fuel-type")
    assert result["ok"]
    assert json.loads(result["content"])["data"] == ROWS


@pytest.fixture(autouse=True)
def no_page_pause(monkeypatch):
    """Pages are paced a full minute apart in production; tests must not wait."""
    monkeypatch.setattr(core, "DE_PAGE_PAUSE_SECONDS", 0)


def test_pages_are_followed_and_concatenated(monkeypatch):
    pages = [ok(page([{"region": "North"}], last=False)),
             ok(page([{"region": "Central"}], last=False)),
             ok(page([{"region": "South"}], last=True))]
    seen = []

    def fake_fetch(endpoint, url):
        seen.append(url)
        return pages[len(seen) - 1]

    monkeypatch.setattr(core, "_fetch", fake_fetch)
    result = core._fetch_all_pages(core.DATA_EXCHANGE_ENDPOINTS[0], "https://x/f")
    regions = [r["region"] for r in json.loads(result["content"])["data"]]
    assert regions == ["North", "Central", "South"]
    assert "pageNumber=1" in seen[0] and "pageNumber=3" in seen[2]


def test_a_page_that_never_says_lastPage_fails_rather_than_truncating(monkeypatch):
    """A partial fuel mix that reads as complete is worse than a failure:
    the RAG lane would publish it with an as-of time and no hint it is short.
    """
    monkeypatch.setattr(core, "_fetch",
                        lambda e, u: ok(page([{"region": "North"}], last=False)))
    result = core._fetch_all_pages(core.DATA_EXCHANGE_ENDPOINTS[0], "https://x/f")
    assert result["ok"] is False
    assert str(core.DE_MAX_PAGES) in result["error"]


def test_a_failed_page_aborts_the_whole_fetch(monkeypatch):
    """Half a payload must never be written."""
    calls = []

    def fake_fetch(endpoint, url):
        calls.append(url)
        if len(calls) == 1:
            return ok(page([{"region": "North"}], last=False))
        return core._fetch_failure("HTTP 401", 401)

    monkeypatch.setattr(core, "_fetch", fake_fetch)
    result = core._fetch_all_pages(core.DATA_EXCHANGE_ENDPOINTS[0], "https://x/f")
    assert result["ok"] is False
    assert result["error"] == "HTTP 401"


def test_an_assembled_payload_still_passes_the_shape_gate(monkeypatch):
    """Rows are concatenated by hand, so the gate runs on the result too."""
    monkeypatch.setattr(core, "_fetch", lambda e, u: ok({"data": [{"nope": 1}],
                                                         "page": {"lastPage": True}}))
    result = core._fetch_all_pages(core.DATA_EXCHANGE_ENDPOINTS[0], "https://x/f")
    assert result["ok"] is False


# --- page URLs -----------------------------------------------------------

def test_absent_paging_metadata_fails_rather_than_assuming_completion(monkeypatch):
    """A page with no lastPage is malformed, not finished.

    Treating it as finished would store page one as though it were the whole
    day - the silent truncation this whole function exists to prevent.
    """
    monkeypatch.setattr(core, "_fetch",
                        lambda e, u: ok({"data": [{"region": "North"}], "page": {}}))
    result = core._fetch_all_pages(core.DATA_EXCHANGE_ENDPOINTS[0], "https://x/f")
    assert result["ok"] is False
    assert result["error"] == "paging metadata missing"


def test_a_response_with_no_page_object_at_all_also_fails(monkeypatch):
    monkeypatch.setattr(core, "_fetch",
                        lambda e, u: ok({"data": [{"region": "North"}]}))
    assert core._fetch_all_pages(core.DATA_EXCHANGE_ENDPOINTS[0],
                                 "https://x/f")["ok"] is False


def test_pages_are_paced_apart(monkeypatch):
    """One guard lease covers the endpoint, so pacing between pages is ours.

    MISO allows about one request per minute to a link; firing pages
    back-to-back inside a single lease is how that gets breached.
    """
    monkeypatch.setattr(core, "DE_PAGE_PAUSE_SECONDS", 7)
    slept = []
    monkeypatch.setattr(core.time, "sleep", slept.append)
    pages = [ok(page([{"region": "North"}], last=False)),
             ok(page([{"region": "South"}], last=True))]
    monkeypatch.setattr(core, "_fetch", lambda e, u: pages[len(slept)])
    core._fetch_all_pages(core.DATA_EXCHANGE_ENDPOINTS[0], "https://x/f")
    assert slept == [7]      # paused before page 2, not before page 1


def test_pages_are_paced_by_the_guards_own_interval():
    """The pause is the whole rate limit for a paged fetch, so it may not be
    shorter than the guard's interval.

    The constant used to be 2 seconds beside a comment citing MISO's
    ~1-request-per-endpoint-per-minute rule, which is a comment describing a
    limit the code ignored. Five pages two seconds apart is five requests to
    one link inside ten seconds, and the penalty for that is an IP ban.
    """
    assert REAL_PAGE_PAUSE_SECONDS >= guard.MIN_SECONDS_BETWEEN


def test_a_worst_case_paged_fetch_still_fits_inside_one_poll_cycle():
    """The arithmetic that makes a full-minute pause affordable: the worst case
    is DE_MAX_PAGES pages, so DE_MAX_PAGES - 1 pauses, inside the 300 s cadence.
    """
    worst_case = (core.DE_MAX_PAGES - 1) * REAL_PAGE_PAUSE_SECONDS
    assert worst_case < core.DEFAULT_POLL_SECONDS


def test_page_parameters_are_appended():
    url = core._page_url("https://x/fuel-type", 2)
    assert "pageNumber=2" in url and f"pageSize={core.DE_PAGE_SIZE}" in url


def test_page_parameters_join_an_existing_query_string():
    url = core._page_url("https://x/f?region=NORTH", 1)
    assert url.count("?") == 1 and "&pageNumber=1" in url


def test_the_paged_endpoints_rotate_so_the_budget_does_not_starve_the_same_ones():
    """The budget is spent in list order, so a fixed order starves the tail
    every cycle rather than the "retries next cycle" the budget promises -
    the first two would win forever and the other nine never refresh."""
    core._paged_rotation = 0
    endpoints = core.ENDPOINTS + core.DATA_EXCHANGE_ENDPOINTS
    firsts = []
    for _ in range(len(core.DATA_EXCHANGE_ENDPOINTS)):
        order = core.cycle_order(endpoints)
        assert [e.key for e in order[:4]] == [e.key for e in core.ENDPOINTS]
        firsts.append(order[4].key)
    # every paged endpoint gets first crack within one full rotation
    assert set(firsts) == {e.key for e in core.DATA_EXCHANGE_ENDPOINTS}


def test_an_unpaged_only_cycle_keeps_its_order():
    core._paged_rotation = 0
    order = core.cycle_order(list(core.ENDPOINTS))
    assert [e.key for e in order] == [e.key for e in core.ENDPOINTS]


def test_a_data_exchange_payload_survives_a_cycle_without_a_key(tmp_path):
    """A key that is unset - or expires mid-demo - makes eleven endpoints
    inactive without removing them from the table. Pruning their payloads
    would strand the documents already in Chroma: present, unrefreshable, with
    no source on disk. Every other prune test passes a key that genuinely left
    the code, so none of them exercised this."""
    (tmp_path / "DEFuelMix.json").write_bytes(b"{}")
    (tmp_path / "DEActualLoad.json").write_bytes(b"{}")
    core._prune_payloads(tmp_path, {"DEFuelMix": {}, "DEActualLoad": {}},
                         {"FuelMix": {}})
    assert (tmp_path / "DEFuelMix.json").exists()
    assert (tmp_path / "DEActualLoad.json").exists()


def test_an_endpoint_that_really_left_the_code_is_still_pruned(tmp_path):
    """The other half: the guard above must not turn pruning off entirely."""
    (tmp_path / "RetiredFeed.json").write_bytes(b"{}")
    core._prune_payloads(tmp_path, {"RetiredFeed": {}}, {"FuelMix": {}})
    assert not (tmp_path / "RetiredFeed.json").exists()


def test_the_forecast_endpoint_is_asked_for_today_not_the_market_day():
    """market_date() is yesterday because the settled endpoints 400 on today.
    Applied blanket, it stored a forecast for a day that had already ended,
    and "what is the load forecast?" was answered from it with today's date on
    the chart. The forecast endpoint does serve the current date."""
    forecast = next(e for e in core.DATA_EXCHANGE_ENDPOINTS if e.key == "DELoadForecast")
    assert forecast.date_mode == "today"
    assert core.endpoint_url(forecast, "") == f"/lgi/v1/forecast/{core.today_est()}/load"


def test_every_other_data_exchange_endpoint_still_asks_for_the_market_day():
    """The settled feeds answer 400 for today; only the forecast is exempt."""
    for endpoint in core.DATA_EXCHANGE_ENDPOINTS:
        if endpoint.key == "DELoadForecast":
            continue
        assert endpoint.date_mode == "market", endpoint.key
        assert core.market_date() in core.endpoint_url(endpoint, ""), endpoint.key
