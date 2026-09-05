"""Shared pytest fixtures.

The one thing every test in this suite must have is an isolated cache
directory. backend.poller.guard keeps its rate-limit lease at
$XDG_CACHE_HOME/miso-copilot/rate-guard.json, on purpose outside the repo
and outside data/, which means an unisolated test would read and rewrite
the developer's real lease file and hand a running poller a lease it never
claimed. The fixture below is autouse so that isolation is not something a
test author has to remember.
"""

from urllib.parse import urlsplit

import pytest
import requests

from tests.support import FakeResponse, GOOD_BODIES

# The `slow` marker is declared in pytest.ini, which --strict-markers reads.
# Registering it a second time here would be dead code at best and a second,
# disagreeing description at worst.


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
# `fake_get` fixture and open no socket at all; the backstop further down
# enforces that rather than trusting it.


# Every environment variable the ingestion lane reads apart from
# XDG_CACHE_HOME, which `cache_dir` owns. Cleared before each test so a
# developer's own shell cannot change what a test asserts.
POLLER_ENV_VARS = (
    "MISO_API_BASE",
    "MISO_RAW_DIR",
    "MISO_POLL_SECONDS",
    "MISO_POLLER_ENABLED",
)


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


# --- the backstop -----------------------------------------------------------
#
# `fake_get` replaces requests.get per test, and every test that fetches is
# supposed to use it. Supposed to is not an invariant: a scheduler test starts
# a daemon thread that APScheduler shuts down with wait=False, so the thread
# can still be alive after monkeypatch has restored the real core.poll_once
# and unset MISO_RAW_DIR. Measured, not hypothetical - at that instant
# base_url() resolves to https://public-api.misoenergy.org and raw_dir() to
# the repo's own data/raw.
#
# So requests.get is replaced here at import time, at module scope, where no
# fixture teardown reaches it. Loopback passes through to the real requests
# for the stub-server tests; anything else raises. monkeypatch.setattr in
# `fake_get` still shadows this per test and restores back to it afterwards.

_real_get = requests.get
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _refuse_non_loopback(url, **kwargs):
    """The real requests.get for loopback; AssertionError for anything else."""
    host = urlsplit(url).hostname or ""
    if host not in _LOOPBACK:
        raise AssertionError(
            f"a test tried to reach {host!r} - the suite contacts no host "
            f"but a stub on 127.0.0.1 (url={url!r})")
    return _real_get(url, **kwargs)


requests.get = _refuse_non_loopback


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
