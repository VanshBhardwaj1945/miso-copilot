"""The per-IP rate limiter and the request log.

This is the only thing standing between a public URL and an exhausted Claude
budget, and it had no tests at all. Two properties matter more than the rest:
the window admits exactly MAX_PER_MINUTE and no more even under concurrency,
and a spoofed X-Forwarded-For cannot mint a fresh bucket.
"""

import json
import threading
import time

import pytest

from backend import security


class FakeRequest:
    """Enough of a Starlette Request for client_ip: headers and client.host."""

    class _Client:
        def __init__(self, host):
            self.host = host

    def __init__(self, headers=None, host="203.0.113.1"):
        self.headers = headers or {}
        self.client = self._Client(host) if host else None


@pytest.fixture(autouse=True)
def clean_buckets():
    """The limiter's state is module-level, so tests must not leak into each other."""
    security._hits.clear()
    yield
    security._hits.clear()


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "requests.jsonl"
    monkeypatch.setattr(security, "LOG_PATH", path)
    return path


# --- client_ip ------------------------------------------------------------

def test_the_socket_address_is_the_bucket_by_default():
    assert security.client_ip(FakeRequest(host="198.51.100.9")) == "198.51.100.9"


def test_a_spoofed_forwarded_header_is_ignored():
    """Anyone can send X-Forwarded-For. Honoring it unconditionally would hand
    every caller an unlimited supply of fresh rate-limit buckets.
    """
    req = FakeRequest({"x-forwarded-for": "1.2.3.4"}, host="198.51.100.9")
    assert security.client_ip(req) == "198.51.100.9"


def test_the_header_is_honored_only_behind_a_trusted_proxy(monkeypatch):
    monkeypatch.setenv("MISO_TRUST_PROXY", "1")
    req = FakeRequest({"x-forwarded-for": "1.2.3.4"}, host="10.0.0.1")
    assert security.client_ip(req) == "1.2.3.4"


def test_only_the_first_hop_of_the_header_is_used(monkeypatch):
    """The rest of the chain is whatever the intermediate proxies claimed."""
    monkeypatch.setenv("MISO_TRUST_PROXY", "1")
    req = FakeRequest({"x-forwarded-for": " 1.2.3.4 , 5.6.7.8"}, host="10.0.0.1")
    assert security.client_ip(req) == "1.2.3.4"


def test_a_request_with_no_client_is_not_a_crash():
    """Starlette leaves request.client None for some transports."""
    assert security.client_ip(FakeRequest(host=None)) == "unknown"


def test_a_trusted_but_empty_header_falls_back_to_the_socket(monkeypatch):
    monkeypatch.setenv("MISO_TRUST_PROXY", "1")
    assert security.client_ip(FakeRequest({"x-forwarded-for": ""},
                                          host="198.51.100.9")) == "198.51.100.9"


# --- allow ----------------------------------------------------------------

def test_the_window_admits_exactly_the_limit_then_blocks():
    ip = "198.51.100.10"
    assert all(security.allow(ip) for _ in range(security.MAX_PER_MINUTE))
    assert security.allow(ip) is False


def test_one_ip_being_blocked_does_not_block_another():
    noisy, quiet = "198.51.100.11", "198.51.100.12"
    for _ in range(security.MAX_PER_MINUTE):
        security.allow(noisy)
    assert security.allow(noisy) is False
    assert security.allow(quiet) is True


def test_the_window_slides(monkeypatch):
    """Old hits age out, so a blocked caller recovers without a restart."""
    ip = "198.51.100.13"
    clock = [1000.0]
    monkeypatch.setattr(security.time, "monotonic", lambda: clock[0])
    for _ in range(security.MAX_PER_MINUTE):
        security.allow(ip)
    assert security.allow(ip) is False
    clock[0] += 61
    assert security.allow(ip) is True


def test_concurrent_callers_cannot_slip_past_the_limit():
    """Sync FastAPI routes run on a threadpool, so this race is real: without
    the lock every thread reads len(hits) < limit before any of them appends.
    """
    ip = "198.51.100.14"
    granted = []
    lock = threading.Lock()

    def hammer():
        got = security.allow(ip)
        with lock:
            granted.append(got)

    threads = [threading.Thread(target=hammer) for _ in range(60)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == security.MAX_PER_MINUTE


def test_quiet_ips_are_forgotten_so_the_table_cannot_grow_forever(monkeypatch):
    """A day of strangers would otherwise be a slow memory leak."""
    clock = [1000.0]
    monkeypatch.setattr(security.time, "monotonic", lambda: clock[0])
    security.allow("198.51.100.15")
    clock[0] += 120
    security.allow("198.51.100.16")
    assert "198.51.100.15" not in security._hits


# --- log_request ----------------------------------------------------------

def test_a_request_is_logged_as_one_json_line(log_path):
    security.log_request("198.51.100.17", "how much wind?", "answered", 1234)
    entry = json.loads(log_path.read_text().strip())
    assert entry["ip"] == "198.51.100.17"
    assert entry["question"] == "how much wind?"
    assert entry["outcome"] == "answered"
    assert entry["ms"] == 1234
    assert entry["ts"]


def test_the_question_is_truncated(log_path):
    """The log is not a transcript store, and /ask accepts up to 2000 chars."""
    security.log_request("1.1.1.1", "x" * 5000, "answered", 1)
    assert len(json.loads(log_path.read_text().strip())["question"]) == 300


def test_entries_append_rather_than_overwrite(log_path):
    for i in range(3):
        security.log_request("1.1.1.1", f"q{i}", "answered", i)
    assert len(log_path.read_text().strip().splitlines()) == 3


def test_a_logging_failure_never_breaks_an_answer(monkeypatch, log_path):
    """Logging is bookkeeping. A full disk must not turn a good answer into a 500."""
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(security.Path, "mkdir", boom)
    security.log_request("1.1.1.1", "q", "answered", 1)   # must not raise


def test_the_log_directory_is_created_on_demand(log_path):
    assert not log_path.parent.exists()
    security.log_request("1.1.1.1", "q", "answered", 1)
    assert log_path.exists()
