"""The Data Exchange endpoint: registration, auth, {date}, and paging.

No network. Paging is driven by stubbing core._fetch, so these never open a
socket - not even to the 127.0.0.1 stub.

The Data Exchange feed runs *beside* the four legacy display feeds rather than
replacing them, so most of what is protected here is the boundary: with no
subscription key nothing about the old behavior may change.
"""

import json

import pytest

from backend.poller import core


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


def test_env_key_beats_the_dotenv_value(monkeypatch):
    monkeypatch.setattr(core.config, "MISO_API_KEY", "from-dotenv")
    monkeypatch.setenv("MISO_API_KEY", "from-env")
    assert core.data_exchange_key() == "from-env"


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
    """
    de = core.DATA_EXCHANGE_ENDPOINTS[0]
    url = core.endpoint_url(de, "https://x")
    assert "{date}" not in url
    from datetime import datetime
    from zoneinfo import ZoneInfo
    assert datetime.now(ZoneInfo("EST")).strftime("%Y-%m-%d") in url


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
    assert core._shape_de_fueltype(body) is False


def test_shape_gate_accepts_a_real_page():
    assert core._shape_de_fueltype(page(ROWS)) is True


# --- paging --------------------------------------------------------------

def test_a_single_page_is_returned_whole(monkeypatch):
    monkeypatch.setattr(core, "_fetch", lambda e, u: ok(page(ROWS)))
    de = core.DATA_EXCHANGE_ENDPOINTS[0]
    result = core._fetch_all_pages(de, "https://x/fuel-type")
    assert result["ok"]
    assert json.loads(result["content"])["data"] == ROWS


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

def test_page_parameters_are_appended():
    url = core._page_url("https://x/fuel-type", 2)
    assert "pageNumber=2" in url and f"pageSize={core.DE_PAGE_SIZE}" in url


def test_page_parameters_join_an_existing_query_string():
    url = core._page_url("https://x/f?region=NORTH", 1)
    assert url.count("?") == 1 and "&pageNumber=1" in url
