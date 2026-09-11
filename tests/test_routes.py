"""/ask, /health and /crosswalk.csv through the real FastAPI app.

Claude is never called: claude.answer_question is replaced per test. What is
being protected here is the contract the two UIs depend on -
POST /ask {question} -> {answer, sources[{title,url}], as_of} - and the
mapping from an Anthropic failure to an HTTP status a user can act on.
"""

import csv
import io
import json

import anthropic
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import security
from backend.llm import claude
from backend.routes import crosswalk as crosswalk_route
from backend.routes.ask import router as ask_router
from backend.routes.crosswalk import router as crosswalk_router


@pytest.fixture(autouse=True)
def clean_limiter():
    security._hits.clear()
    yield
    security._hits.clear()


@pytest.fixture(autouse=True)
def quiet_log(tmp_path, monkeypatch):
    """Never append to the real data/logs/requests.jsonl from a test."""
    monkeypatch.setattr(security, "LOG_PATH", tmp_path / "requests.jsonl")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ask_router)
    app.include_router(crosswalk_router)
    return TestClient(app)


@pytest.fixture
def answering(monkeypatch):
    """A configured Claude that returns a fixed answer."""
    monkeypatch.setattr(claude, "client", object())
    monkeypatch.setattr(claude, "answer_question",
                        lambda q: ("**Wind** is 1,500 MW.",
                                   [{"title": "MISO", "url": "https://www.misoenergy.org/"}],
                                   None))


def raises(exc):
    def _raise(_q):
        raise exc
    return _raise


# --- /health --------------------------------------------------------------

def test_health_reports_whether_a_key_is_loaded(client, monkeypatch):
    monkeypatch.setattr(claude, "client", None)
    assert client.get("/health").json() == {"ok": True, "claude_configured": False}
    monkeypatch.setattr(claude, "client", object())
    assert client.get("/health").json()["claude_configured"] is True


# --- /ask happy path ------------------------------------------------------

def test_ask_returns_the_contract_both_uis_depend_on(client, answering):
    body = client.post("/ask", json={"question": "how much wind?"}).json()
    assert set(body) == {"answer", "sources", "as_of"}
    assert body["answer"].startswith("**Wind**")
    assert body["sources"] == [{"title": "MISO", "url": "https://www.misoenergy.org/"}]


def test_the_as_of_stamp_is_fixed_est(client, answering):
    """MISO stamps everything in EST year round, so a DST-aware zone would read
    an hour off MISO's own displays all summer.
    """
    as_of = client.post("/ask", json={"question": "q"}).json()["as_of"]
    assert as_of.endswith(" EST")


def test_the_answer_carries_misos_own_stamp_when_live_data_reached_it(client, monkeypatch):
    """The retriever works out the "as of" from the live feeds that actually
    answered. This route computed its own from the wall clock and dropped that
    value on the floor, so an answer read off yesterday's settled market day
    was stamped with the current time - the freshness claim was never true,
    only never checked."""
    monkeypatch.setattr(claude, "client", object())
    monkeypatch.setattr(claude, "answer_question",
                        lambda q: ("a", [], "10-Sep-2026 - Interval 18:55 EST"))
    assert client.post("/ask", json={"question": "q"}).json()["as_of"] == \
        "10-Sep-2026 - Interval 18:55 EST"


def test_an_answer_with_no_live_data_falls_back_to_the_clock(client, monkeypatch):
    """A question answered entirely from reference documents has no MISO stamp
    to carry, and a blank "as of" reads worse than the time of the reply."""
    monkeypatch.setattr(claude, "client", object())
    monkeypatch.setattr(claude, "answer_question", lambda q: ("a", [], None))
    assert client.post("/ask", json={"question": "q"}).json()["as_of"].endswith(" EST")


def test_a_successful_question_is_logged_as_answered(client, answering, tmp_path):
    client.post("/ask", json={"question": "how much wind?"})
    entry = json.loads((security.LOG_PATH).read_text().strip())
    assert entry["outcome"] == "answered"
    assert entry["ms"] >= 0


# --- /ask input handling --------------------------------------------------

@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "x" * 2001}])
def test_unusable_questions_are_rejected_before_claude(client, payload, monkeypatch):
    """The cap exists so a huge body cannot run up embedding and Claude cost."""
    monkeypatch.setattr(claude, "client", object())
    monkeypatch.setattr(claude, "answer_question",
                        raises(AssertionError("Claude must not be called")))
    assert client.post("/ask", json=payload).status_code == 422


def test_a_question_at_the_cap_is_accepted(client, answering):
    assert client.post("/ask", json={"question": "x" * 2000}).status_code == 200


# --- /ask failure mapping -------------------------------------------------

def test_no_key_is_a_503_not_a_crash(client, monkeypatch):
    monkeypatch.setattr(claude, "client", None)
    r = client.post("/ask", json={"question": "q"})
    assert r.status_code == 503
    assert "CLAUDE_API_KEY" in r.json()["detail"]


def _anthropic_error(cls, status):
    """Build an anthropic error without a live client."""
    import httpx2 as httpx
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return cls("boom", response=response, body=None)


@pytest.mark.parametrize("cls,status,expected", [
    (anthropic.AuthenticationError, 401, 503),
    (anthropic.RateLimitError, 429, 503),
])
def test_recoverable_anthropic_failures_become_503(client, monkeypatch, cls, status, expected):
    monkeypatch.setattr(claude, "client", object())
    monkeypatch.setattr(claude, "answer_question", raises(_anthropic_error(cls, status)))
    assert client.post("/ask", json={"question": "q"}).status_code == expected


def test_an_upstream_server_error_becomes_502(client, monkeypatch):
    monkeypatch.setattr(claude, "client", object())
    monkeypatch.setattr(claude, "answer_question",
                        raises(_anthropic_error(anthropic.InternalServerError, 500)))
    r = client.post("/ask", json={"question": "q"})
    assert r.status_code == 502


def test_an_unreachable_api_becomes_502(client, monkeypatch):
    import httpx2 as httpx
    monkeypatch.setattr(claude, "client", object())
    err = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    monkeypatch.setattr(claude, "answer_question", raises(err))
    r = client.post("/ask", json={"question": "q"})
    assert r.status_code == 502
    assert "reach" in r.json()["detail"].lower()


def test_a_failure_is_logged_as_failed(client, monkeypatch):
    monkeypatch.setattr(claude, "client", object())
    monkeypatch.setattr(claude, "answer_question",
                        raises(_anthropic_error(anthropic.RateLimitError, 429)))
    client.post("/ask", json={"question": "q"})
    assert json.loads(security.LOG_PATH.read_text().strip())["outcome"] == "failed"


# --- /ask rate limiting ---------------------------------------------------

def test_the_limiter_returns_429_and_stops_calling_claude(client, monkeypatch):
    calls = []
    monkeypatch.setattr(claude, "client", object())
    monkeypatch.setattr(claude, "answer_question",
                        lambda q: (calls.append(q), ("a", [], None))[1])
    for _ in range(security.MAX_PER_MINUTE):
        assert client.post("/ask", json={"question": "q"}).status_code == 200
    blocked = client.post("/ask", json={"question": "q"})
    assert blocked.status_code == 429
    assert len(calls) == security.MAX_PER_MINUTE   # the 21st never reached Claude


def test_a_rate_limited_request_is_logged(client, monkeypatch):
    monkeypatch.setattr(claude, "client", object())
    monkeypatch.setattr(claude, "answer_question", lambda q: ("a", [], None))
    for _ in range(security.MAX_PER_MINUTE + 1):
        client.post("/ask", json={"question": "q"})
    outcomes = [json.loads(line)["outcome"]
                for line in security.LOG_PATH.read_text().splitlines()]
    assert outcomes[-1] == "rate_limited"


# --- /crosswalk.csv -------------------------------------------------------

def test_the_crosswalk_downloads_as_a_named_csv(client):
    r = client.get("/crosswalk.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "miso-report-to-api-crosswalk.csv" in r.headers["content-disposition"]


def test_every_row_carries_a_field_mapping_and_its_guide(client):
    """One row per column mapping is the shape a trader pastes into a sheet."""
    rows = list(csv.DictReader(io.StringIO(client.get("/crosswalk.csv").text)))
    assert rows
    assert all(r["old_column"] and r["new_field"] and r["endpoint"] for r in rows)
    assert all(r["readers_guide"].startswith("http") for r in rows)


def test_a_missing_crosswalk_is_a_404_rather_than_a_500(client, monkeypatch, tmp_path):
    monkeypatch.setattr(crosswalk_route, "CROSSWALK_PATH", tmp_path / "gone.json")
    r = client.get("/crosswalk.csv")
    assert r.status_code == 404
    assert "README" in r.json()["detail"]


def test_entries_without_params_or_shape_still_produce_rows(monkeypatch, tmp_path):
    """Optional fields are optional; a sparse entry must not raise."""
    path = tmp_path / "crosswalk.json"
    path.write_text(json.dumps({"entries": [{
        "report": "R", "api": "pricing", "endpoint": "GET /x",
        "base_url": "https://x", "report_url": "https://guide",
        "fields": [{"old": "A", "new": "b"}],
    }]}))
    monkeypatch.setattr(crosswalk_route, "CROSSWALK_PATH", path)
    rows = crosswalk_route.crosswalk_rows()
    assert rows == [{
        "report": "R", "old_file": "", "old_column": "A", "api": "pricing",
        "endpoint": "GET /x", "base_url": "https://x", "new_field": "b",
        "note": "", "params": "", "shape": "", "readers_guide": "https://guide",
    }]
