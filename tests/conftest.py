"""Shared pytest fixtures.

The one thing every test in this suite must have is an isolated cache
directory. backend.poller.guard keeps its rate-limit lease at
$XDG_CACHE_HOME/miso-copilot/rate-guard.json, on purpose outside the repo
and outside data/, which means an unisolated test would read and rewrite
the developer's real lease file and hand a running poller a lease it never
claimed. The fixture below is autouse so that isolation is not something a
test author has to remember.
"""

import pytest


def pytest_configure(config):
    """Register the marks this suite uses, so -W error stays usable."""
    config.addinivalue_line(
        "markers", "slow: takes over a second, usually by forking processes")


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    """Point XDG_CACHE_HOME at a per-test tmp dir and return that dir.

    Autouse: every test gets the redirect whether or not it asks for it.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return cache


# --- ingestion-lane fixtures ------------------------------------------------
#
# The second thing every test in this suite must have is an isolated raw
# directory. Without MISO_RAW_DIR pointed somewhere disposable, a test that
# calls poll_once() writes the repo's own data/raw/ and prunes files out of
# it. `poller_env` below is autouse for the same reason `cache_dir` is.
#
# No test in this suite makes a request to misoenergy.org or to any host
# other than a stub bound to 127.0.0.1. Most drive requests.get through the
# `fake_get` fixture and open no socket at all.

import sys                                          # noqa: E402
from pathlib import Path                            # noqa: E402

import requests                                     # noqa: E402

# The tests import `backend.poller.core` and `tests.stub.server`, both of
# which resolve from the repo root. `python -m pytest` puts the working
# directory on sys.path, but a bare `pytest` does not.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# Every environment variable the ingestion lane reads apart from
# XDG_CACHE_HOME, which `cache_dir` owns. Cleared before each test so a
# developer's own shell cannot change what a test asserts.
POLLER_ENV_VARS = (
    "MISO_API_BASE",
    "MISO_RAW_DIR",
    "MISO_POLL_SECONDS",
    "MISO_POLLER_ENABLED",
)

# Minimal payloads that pass each endpoint's shape gate, keyed by path.
# Hand-written, not captured: no MISO data belongs in the repo.
GOOD_BODIES = {
    "/api/FuelMix": (
        b'{"RefId": "ref-fuelmix", "TotalMW": "1000", '
        b'"Fuel": {"Type": []}}'
    ),
    "/api/RealTimeTotalLoad": (
        b'{"LoadInfo": {"RefId": "ref-load", "LoadValues": []}}'
    ),
    "/api/Snapshot": (
        b'[{"t": "Current Demand (MW)", "v": "1,000", '
        b'"d": "1/01/1970 12:00:00 AM EST", "id": "demand"}]'
    ),
    "/api/WindSolar/GetCombined": (
        b'{"instance": "stub", "RefId": "ref-windsolar", '
        b'"MktDay": "1970-01-01"}'
    ),
}

# The ref_id each GOOD_BODIES payload yields, keyed by endpoint. Snapshot
# carries no RefId at any level and is permanently None.
GOOD_REF_IDS = {
    "FuelMix": "ref-fuelmix",
    "RealTimeTotalLoad": "ref-load",
    "Snapshot": None,
    "WindSolar": "ref-windsolar",
}


@pytest.fixture(autouse=True)
def poller_env(cache_dir, tmp_path, monkeypatch):
    """Point MISO_RAW_DIR at a per-test tmp dir and return that dir.

    Depends on `cache_dir` so the cache redirect is already in place and so
    this never touches XDG_CACHE_HOME itself.
    """
    for name in POLLER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    monkeypatch.setenv("MISO_RAW_DIR", str(raw))
    return raw


@pytest.fixture
def raw(poller_env):
    """The temporary directory MISO_RAW_DIR points at."""
    return poller_env


class FakeResponse:
    """The two attributes core reads off a requests response."""

    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code


class FakeGet:
    """A stand-in for requests.get, routed by the tail of the URL.

    A route value is a FakeResponse, an exception instance to raise, or a
    callable taking the URL. An unrouted URL is a test bug, not a fetch
    failure, so it raises AssertionError rather than quietly 404ing.
    """

    def __init__(self):
        self.routes = {}
        self.calls = []

    def set(self, suffix, value):
        self.routes[suffix] = value

    def set_all(self, value):
        for path in GOOD_BODIES:
            self.routes[path] = value

    def serve_good(self):
        for path, body in GOOD_BODIES.items():
            self.routes[path] = FakeResponse(body, 200)

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for suffix, value in self.routes.items():
            if url.endswith(suffix):
                if callable(value):
                    value = value(url)
                if isinstance(value, BaseException):
                    raise value
                return value
        raise AssertionError(f"no route configured for {url}")


@pytest.fixture
def fake_get(monkeypatch):
    """requests.get replaced by a routing table. Opens no socket."""
    fake = FakeGet()
    monkeypatch.setattr(requests, "get", fake)
    return fake


@pytest.fixture
def good_get(fake_get):
    """fake_get already serving a valid payload for all four endpoints."""
    fake_get.serve_good()
    return fake_get


@pytest.fixture
def stub_base():
    """A stub server on 127.0.0.1. Yields a factory taking stub modes.

    Loopback, so the rate guard is bypassed and no lease file is written.
    """
    servers = []

    def start(modes=None, rotate_ref_id=True):
        from tests.stub.server import serve_in_thread

        httpd, _thread = serve_in_thread(modes=modes,
                                         rotate_ref_id=rotate_ref_id)
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_port}"

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()
