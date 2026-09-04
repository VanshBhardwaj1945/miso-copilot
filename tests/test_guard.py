"""Tests for backend.poller.guard, the per-link rate guard.

This module is the only thing standing between the poller and an IP ban
from MISO, so the target here is total coverage of every row of the
decision table in `_decide`, every fail-closed path in `claim`, and the
cross-process locking that is the reason the module exists at all.

Two rules hold for the whole file:

  * No test makes a network request. The module never does, and neither
    does anything here.
  * Every test runs with XDG_CACHE_HOME pointed at a tmp dir by the
    autouse `cache_dir` fixture in conftest.py, so the developer's real
    ~/.cache/miso-copilot/rate-guard.json is never opened.

Tests that exist because of a specific bug say so in a one-line comment.
Those are the load-bearing ones; the rest describe the contract.
"""

import json
import logging
import multiprocessing
import os
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.poller import guard
from backend.poller.guard import GuardUnreadableError

URL = "https://public-api.misoenergy.org/api/FuelMix"
OTHER_URL = "https://public-api.misoenergy.org/api/LMP"

# A fixed instant to measure ages against, so no test depends on how long
# it took to run. Offset-aware, because the guard rejects naive stamps.
NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def lease_path(cache_dir):
    """The lease file the guard will use under the isolated cache dir."""
    return cache_dir / "miso-copilot" / "rate-guard.json"


@pytest.fixture(autouse=True)
def _cache_is_isolated(lease_path):
    """Refuse to run any test whose guard path is not the tmp one.

    A safety net, not a behavior test: if the isolation fixture is ever
    dropped, this fails loudly instead of writing the developer's file.
    """
    assert guard.guard_path() == lease_path


@pytest.fixture
def write_lease(lease_path):
    """Write the lease file holding one raw JSON value for a URL."""
    def _write(value, url=URL):
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(json.dumps({url: value}))
        return lease_path
    return _write


@pytest.fixture
def lease_aged(write_lease):
    """Write a lease for URL stamped `age` before the frozen NOW."""
    def _aged(age, url=URL):
        return write_lease((NOW - age).isoformat(), url=url)
    return _aged


@pytest.fixture
def clock(monkeypatch):
    """Freeze guard._now at NOW, with .advance() to move it forward."""
    state = {"now": NOW}
    monkeypatch.setattr(guard, "_now", lambda: state["now"])
    return types.SimpleNamespace(
        now=lambda: state["now"],
        advance=lambda delta: state.__setitem__("now", state["now"] + delta),
    )


@pytest.fixture
def hold_lock():
    """Hold the flock on a lease file's sidecar from a second handle.

    flock is per open file description, so a second open() in this same
    process contends exactly as another process would. Cheaper than
    forking for the tests that only need the lock to be unavailable.
    """
    handles = []

    def _hold(path):
        import fcntl
        lock_file = Path(path).with_suffix(".lock")
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_file, "w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handles.append(handle)
        return handle

    yield _hold
    for handle in handles:
        handle.close()


# --- guard_path -------------------------------------------------------------

def test_guard_path_honors_xdg_cache_home(cache_dir):
    assert guard.guard_path() == (
        cache_dir / "miso-copilot" / "rate-guard.json")


def test_guard_path_falls_back_to_home_cache_when_xdg_is_unset(monkeypatch):
    # Computes a path only; nothing here opens the real lease file.
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert guard.guard_path() == (
        Path.home() / ".cache" / "miso-copilot" / "rate-guard.json")


def test_guard_path_falls_back_to_home_cache_when_xdg_is_empty(monkeypatch):
    # An exported-but-empty XDG_CACHE_HOME must not resolve to "/miso-copilot".
    monkeypatch.setenv("XDG_CACHE_HOME", "")
    assert guard.guard_path() == (
        Path.home() / ".cache" / "miso-copilot" / "rate-guard.json")


# --- _decide: the decision table --------------------------------------------

def test_absent_lease_proceeds():
    allowed, warning = guard._decide(None, NOW)
    assert (allowed, warning) == (True, None)


def test_lease_59_seconds_ago_is_denied():
    allowed, _ = guard._decide((NOW - timedelta(seconds=59)).isoformat(), NOW)
    assert allowed is False


def test_lease_exactly_60_seconds_ago_is_allowed():
    # 60 s is the rule itself: at the boundary the request goes through.
    allowed, _ = guard._decide((NOW - timedelta(seconds=60)).isoformat(), NOW)
    assert allowed is True


def test_lease_61_seconds_ago_is_allowed():
    allowed, _ = guard._decide((NOW - timedelta(seconds=61)).isoformat(), NOW)
    assert allowed is True


def test_lease_between_60_seconds_and_1_hour_ago_warns_about_nothing():
    _, warning = guard._decide((NOW - timedelta(seconds=61)).isoformat(), NOW)
    assert warning is None


def test_lease_59_minutes_ago_is_allowed():
    allowed, _ = guard._decide((NOW - timedelta(minutes=59)).isoformat(), NOW)
    assert allowed is True


def test_lease_59_minutes_ago_does_not_warn():
    _, warning = guard._decide((NOW - timedelta(minutes=59)).isoformat(), NOW)
    assert warning is None


def test_lease_exactly_60_minutes_ago_does_not_warn():
    # STALE_AFTER is exclusive: exactly one hour is not yet "poller down".
    _, warning = guard._decide((NOW - timedelta(minutes=60)).isoformat(), NOW)
    assert warning is None


def test_lease_exactly_60_minutes_ago_is_allowed():
    allowed, _ = guard._decide((NOW - timedelta(minutes=60)).isoformat(), NOW)
    assert allowed is True


def test_lease_61_minutes_ago_is_allowed():
    allowed, _ = guard._decide((NOW - timedelta(minutes=61)).isoformat(), NOW)
    assert allowed is True


def test_lease_61_minutes_ago_warns_that_the_poller_was_down():
    _, warning = guard._decide((NOW - timedelta(minutes=61)).isoformat(), NOW)
    assert "poller was down" in warning


def test_lease_4_9_seconds_in_the_future_is_denied():
    # The race artifact, not a clock jump: another process claimed this
    # link milliseconds ago and its stamp is barely ahead of ours.
    stored = (NOW + timedelta(seconds=4.9)).isoformat()
    allowed, _ = guard._decide(stored, NOW)
    assert allowed is False


def test_lease_exactly_5_seconds_in_the_future_is_denied():
    # The tolerance is exclusive: at exactly 5 s we do not believe the clock.
    stored = (NOW + timedelta(seconds=5)).isoformat()
    allowed, _ = guard._decide(stored, NOW)
    assert allowed is False


def test_lease_5_1_seconds_in_the_future_is_allowed():
    stored = (NOW + timedelta(seconds=5.1)).isoformat()
    allowed, _ = guard._decide(stored, NOW)
    assert allowed is True


def test_lease_5_1_seconds_in_the_future_warns_about_the_future():
    stored = (NOW + timedelta(seconds=5.1)).isoformat()
    _, warning = guard._decide(stored, NOW)
    assert "is in the future" in warning


def test_lease_far_in_the_future_is_allowed():
    # A believable backward clock step must never wedge the poller.
    stored = (NOW + timedelta(days=3)).isoformat()
    allowed, _ = guard._decide(stored, NOW)
    assert allowed is True


def test_unparseable_lease_is_allowed():
    allowed, _ = guard._decide("not-a-timestamp", NOW)
    assert allowed is True


def test_unparseable_lease_warns():
    _, warning = guard._decide("not-a-timestamp", NOW)
    assert "unparseable lease timestamp" in warning


def test_naive_lease_is_allowed():
    allowed, _ = guard._decide("2026-09-04T12:00:00", NOW)
    assert allowed is True


def test_naive_lease_warns():
    _, warning = guard._decide("2026-09-04T12:00:00", NOW)
    assert "naive lease timestamp" in warning


@pytest.mark.parametrize("stored", [7, 1.5, True, {"at": "now"}, ["now"]],
                         ids=["int", "float", "bool", "dict", "list"])
def test_non_string_lease_is_allowed(stored):
    # fromisoformat raises TypeError, not ValueError, on a non-string; the
    # lease file is hand-editable JSON so both have to be caught.
    allowed, _ = guard._decide(stored, NOW)
    assert allowed is True


@pytest.mark.parametrize("stored", [7, 1.5, True, {"at": "now"}, ["now"]],
                         ids=["int", "float", "bool", "dict", "list"])
def test_non_string_lease_warns(stored):
    _, warning = guard._decide(stored, NOW)
    assert "unparseable lease timestamp" in warning


def test_json_null_lease_is_treated_as_absent():
    # A stored JSON null is indistinguishable from a missing key, so it
    # proceeds silently rather than with the "unparseable" warning the
    # other non-string values get. Asserted so the asymmetry is deliberate.
    assert guard._decide(None, NOW) == (True, None)


# --- claim: the happy path --------------------------------------------------

def test_first_claim_with_no_lease_file_proceeds(lease_path):
    assert lease_path.exists() is False
    assert guard.claim(URL) is True


def test_first_claim_creates_the_lease_file(lease_path):
    guard.claim(URL)
    assert lease_path.exists() is True


def test_claim_writes_the_timestamp_before_it_returns(clock, lease_path):
    # The claim is recorded before the caller fetches, so a process killed
    # mid-fetch still leaves its lease behind. On disk by the time claim
    # returns True is exactly that guarantee.
    assert guard.claim(URL) is True
    assert json.loads(lease_path.read_text())[URL] == NOW.isoformat()


def test_claim_reads_the_clock_after_taking_the_lock(monkeypatch):
    # Load-bearing. A caller-supplied `now`, read before the lock, let the
    # process that won the lock second compare against a moment older than
    # the winner's fresh lease, read that as a clock jump, and fetch too.
    # Racing four processes on one link produced two or three winners.
    order = []
    real_lock, real_now = guard._lock, guard._now
    monkeypatch.setattr(
        guard, "_lock", lambda p: (order.append("lock"), real_lock(p))[1])
    monkeypatch.setattr(
        guard, "_now", lambda: (order.append("now"), real_now())[1])
    guard.claim(URL)
    assert order == ["lock", "now"]


def test_claim_leaves_other_links_leases_alone(clock, write_lease, lease_path):
    write_lease((NOW - timedelta(hours=5)).isoformat(), url=OTHER_URL)
    guard.claim(URL)
    leases = json.loads(lease_path.read_text())
    assert leases[OTHER_URL] == (NOW - timedelta(hours=5)).isoformat()


def test_second_claim_within_60_seconds_is_denied(clock):
    guard.claim(URL)
    clock.advance(timedelta(seconds=59))
    assert guard.claim(URL) is False


def test_denied_claim_does_not_overwrite_the_stored_lease(clock, lease_path):
    guard.claim(URL)
    stored = lease_path.read_text()
    clock.advance(timedelta(seconds=59))
    guard.claim(URL)
    assert lease_path.read_text() == stored


def test_denied_claim_logs_a_warning(clock, caplog):
    guard.claim(URL)
    clock.advance(timedelta(seconds=59))
    with caplog.at_level(logging.WARNING, logger="backend.poller.guard"):
        guard.claim(URL)
    assert "skipping" in caplog.text


def test_claim_after_60_seconds_is_allowed(clock):
    guard.claim(URL)
    clock.advance(timedelta(seconds=60))
    assert guard.claim(URL) is True


def test_claim_on_a_different_link_is_not_blocked_by_a_fresh_lease(clock):
    guard.claim(URL)
    assert guard.claim(OTHER_URL) is True


def test_a_granted_claim_restarts_the_60_second_window(clock):
    # The guard's whole job. Every other test here grants once, or denies
    # once; this is the only one that grants twice, and it is what pins the
    # lease being *rewritten* on a grant rather than merely written when
    # absent. Without it, `leases[url] = ...` can become setdefault() and the
    # suite stays green while the guard grants every claim forever after the
    # first minute - which is the IP ban the module exists to prevent.
    assert guard.claim(URL) is True
    clock.advance(timedelta(seconds=60))
    assert guard.claim(URL) is True
    clock.advance(timedelta(seconds=59))
    assert guard.claim(URL) is False


def test_a_granted_claim_rewrites_the_stored_lease(clock, lease_path):
    # The same contract read off the file rather than the return value.
    guard.claim(URL)
    first = json.loads(lease_path.read_text())[URL]
    clock.advance(timedelta(seconds=60))
    guard.claim(URL)
    assert json.loads(lease_path.read_text())[URL] != first


def test_stale_lease_claim_logs_the_poller_was_down_warning(
        clock, lease_aged, caplog):
    lease_aged(timedelta(hours=2))
    with caplog.at_level(logging.WARNING, logger="backend.poller.guard"):
        assert guard.claim(URL) is True
    assert "poller was down" in caplog.text


# --- claim: fail closed -----------------------------------------------------

# A chmod-based denial is a no-op for root, which can read a 0o000 file
# regardless. CI runs as an unprivileged user, but a devcontainer or a
# `sudo pytest` would otherwise turn these red with a confusing message.
needs_unprivileged = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root can read a 0o000 file, so the denial cannot be provoked")


@needs_unprivileged
def test_unreadable_lease_file_denies_the_claim(lease_path):
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text("{}")
    os.chmod(lease_path, 0o000)
    try:
        assert guard.claim(URL) is False
    finally:
        os.chmod(lease_path, 0o600)


@needs_unprivileged
def test_unreadable_lease_file_raises_guard_unreadable_error(lease_path):
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text("{}")
    os.chmod(lease_path, 0o000)
    try:
        with pytest.raises(GuardUnreadableError, match="cannot read"):
            guard._read(lease_path)
    finally:
        os.chmod(lease_path, 0o600)


@needs_unprivileged
def test_unreadable_lease_file_logs_an_error(lease_path, caplog):
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text("{}")
    os.chmod(lease_path, 0o000)
    try:
        with caplog.at_level(logging.ERROR, logger="backend.poller.guard"):
            guard.claim(URL)
    finally:
        os.chmod(lease_path, 0o600)
    assert "refusing to fetch" in caplog.text


def test_missing_lease_file_reads_as_an_empty_set_of_leases(lease_path):
    # Missing is first run, not corruption; it must not fail closed.
    assert guard._read(lease_path) == {}


def test_invalid_json_lease_file_denies_the_claim(lease_path):
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text("{not json")
    assert guard.claim(URL) is False


def test_zero_byte_lease_file_denies_the_claim(lease_path):
    # A truncated write leaves an empty file; forgetting every lease there
    # would fetch all four links at once, the exact burst this prevents.
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text("")
    assert guard.claim(URL) is False


@pytest.mark.parametrize("body", ["[]", "null", '"x"', "123", "true"],
                         ids=["list", "null", "string", "int", "bool"])
def test_lease_file_that_is_not_a_json_object_denies_the_claim(
        lease_path, body):
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text(body)
    assert guard.claim(URL) is False


@pytest.mark.parametrize("body", ["[]", "null", '"x"', "123", "true"],
                         ids=["list", "null", "string", "int", "bool"])
def test_lease_file_that_is_not_a_json_object_raises(lease_path, body):
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text(body)
    with pytest.raises(GuardUnreadableError, match="not a JSON object"):
        guard._read(lease_path)


def test_lease_file_that_is_a_directory_denies_the_claim(lease_path):
    lease_path.mkdir(parents=True)
    assert guard.claim(URL) is False


def test_deeply_nested_lease_file_denies_the_claim(lease_path):
    # json raises RecursionError rather than ValueError here, which is why
    # the module catches both; catching only ValueError crashed the cycle.
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text("[" * 200_000 + "]" * 200_000)
    assert guard.claim(URL) is False


def test_cache_directory_that_cannot_be_created_denies_the_claim(
        monkeypatch, tmp_path):
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("this is a file, so mkdir under it cannot work")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocked))
    assert guard.claim(URL) is False


def test_cache_directory_that_cannot_be_created_logs_an_error(
        monkeypatch, tmp_path, caplog):
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("this is a file, so mkdir under it cannot work")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocked))
    with caplog.at_level(logging.ERROR, logger="backend.poller.guard"):
        guard.claim(URL)
    assert "cannot create" in caplog.text


def test_lease_that_cannot_be_written_denies_the_claim(lease_path):
    # Read-only cache dir: the lock file already exists so it can still be
    # opened, but write_atomic's temp file cannot be created.
    lease_path.parent.mkdir(parents=True)
    lease_path.with_suffix(".lock").touch()
    os.chmod(lease_path.parent, 0o500)
    try:
        assert guard.claim(URL) is False
    finally:
        os.chmod(lease_path.parent, 0o700)


def test_lease_that_cannot_be_written_logs_an_error(lease_path, caplog):
    lease_path.parent.mkdir(parents=True)
    lease_path.with_suffix(".lock").touch()
    os.chmod(lease_path.parent, 0o500)
    try:
        with caplog.at_level(logging.ERROR, logger="backend.poller.guard"):
            guard.claim(URL)
    finally:
        os.chmod(lease_path.parent, 0o700)
    assert "cannot write" in caplog.text


def test_lock_that_cannot_be_taken_denies_the_claim(
        monkeypatch, lease_path, hold_lock):
    monkeypatch.setattr(guard, "LOCK_TIMEOUT_SECONDS", 0)
    hold_lock(lease_path)
    assert guard.claim(URL) is False


def test_lock_that_cannot_be_taken_logs_an_error(
        monkeypatch, lease_path, hold_lock, caplog):
    monkeypatch.setattr(guard, "LOCK_TIMEOUT_SECONDS", 0)
    hold_lock(lease_path)
    with caplog.at_level(logging.ERROR, logger="backend.poller.guard"):
        guard.claim(URL)
    assert "cannot lock" in caplog.text


# --- the lock is released on every path -------------------------------------

def test_lock_is_released_after_a_fail_closed_denial(monkeypatch, lease_path):
    # A leaked lock would wedge every later claim for LOCK_TIMEOUT_SECONDS
    # and then fail closed forever. flock is per open file description, so
    # re-acquiring from this same process is a real check.
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text("{not json")
    assert guard.claim(URL) is False
    monkeypatch.setattr(guard, "LOCK_TIMEOUT_SECONDS", 0)
    handle = guard._lock(lease_path)
    guard._unlock(handle)


def test_lock_is_released_when_the_body_raises(monkeypatch, lease_path):
    monkeypatch.setattr(
        guard, "_now", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        guard.claim(URL)
    monkeypatch.setattr(guard, "LOCK_TIMEOUT_SECONDS", 0)
    handle = guard._lock(lease_path)
    guard._unlock(handle)


def test_claim_succeeds_again_after_an_unreadable_lease_file(
        clock, lease_path):
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text("{not json")
    assert guard.claim(URL) is False
    lease_path.write_text("{}")
    assert guard.claim(URL) is True


def test_lock_retries_while_held_and_succeeds_once_released(
        lease_path, hold_lock):
    import threading
    import time
    lease_path.parent.mkdir(parents=True)
    handle = hold_lock(lease_path)
    threading.Timer(0.2, handle.close).start()
    started = time.monotonic()
    acquired = guard._lock(lease_path)
    waited = time.monotonic() - started
    guard._unlock(acquired)
    # The assertion is the point: without it this test passes just as well
    # when _lock never blocks at all, and it is the only test of the retry
    # loop. Acquiring the lock only after the holder released it is the
    # behavior, so the call has to have taken at least that long.
    assert waited >= 0.2


def test_lock_raises_once_the_timeout_expires(
        monkeypatch, lease_path, hold_lock):
    monkeypatch.setattr(guard, "LOCK_TIMEOUT_SECONDS", 0)
    hold_lock(lease_path)
    with pytest.raises(OSError):
        guard._lock(lease_path)


def test_unlock_closes_the_handle_it_was_given(lease_path):
    lease_path.parent.mkdir(parents=True)
    handle = guard._lock(lease_path)
    guard._unlock(handle)
    assert handle.closed is True


def test_unlock_of_an_already_closed_handle_does_not_raise(lease_path):
    # _unlock's docstring promises it never raises. It used to suppress
    # OSError only, and fileno() on a closed handle raises ValueError, so a
    # double unlock crashed. Nothing reaches it today, which is exactly why
    # a later edit could reintroduce it unnoticed.
    lease_path.parent.mkdir(parents=True)
    handle = guard._lock(lease_path)
    handle.close()
    guard._unlock(handle)


def test_unlock_twice_does_not_raise(lease_path):
    lease_path.parent.mkdir(parents=True)
    handle = guard._lock(lease_path)
    guard._unlock(handle)
    guard._unlock(handle)


# --- concurrency: the reason the module exists ------------------------------

def _race_worker(url, cache_home):
    """Child process: claim `url` and exit 0 only if it won the lease."""
    os.environ["XDG_CACHE_HOME"] = cache_home
    from backend.poller import guard as child_guard
    os._exit(0 if child_guard.claim(url) else 1)


def _race(urls, cache_home):
    """Fork one child per URL, all at once, and return their exit codes."""
    # fork, not spawn: the lock is fcntl.flock, so the racers have to be
    # real processes, and fork keeps 60 of them under two seconds.
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_race_worker, args=(url, str(cache_home)))
             for url in urls]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(30)
    return [proc.exitcode for proc in procs]


def test_two_processes_claiming_one_url_produce_exactly_one_winner(cache_dir):
    codes = _race([URL, URL], cache_dir)
    assert codes.count(0) == 1


@pytest.mark.slow
@pytest.mark.parametrize("trial", range(10))
def test_six_processes_racing_one_url_produce_exactly_one_winner(
        tmp_path, monkeypatch, trial):
    # Load-bearing. Before the clock was read inside the lock, a race like
    # this produced two or three winners and put three requests on one
    # link inside a second. One trial cannot see that; ten can.
    cache = tmp_path / f"race-{trial}"
    cache.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    codes = _race([URL] * 6, cache)
    assert codes.count(0) == 1


def test_processes_claiming_different_urls_all_win(cache_dir):
    urls = [f"https://public-api.misoenergy.org/api/Link{i}" for i in range(6)]
    codes = _race(urls, cache_dir)
    assert codes.count(0) == 6


def test_processes_claiming_different_urls_do_not_block_each_other(cache_dir):
    # The lock covers the read-decide-write and nothing else. Six claims
    # serialize on it, but for microseconds, not LOCK_TIMEOUT_SECONDS.
    urls = [f"https://public-api.misoenergy.org/api/Link{i}" for i in range(6)]
    started = time.monotonic()
    _race(urls, cache_dir)
    assert time.monotonic() - started < guard.LOCK_TIMEOUT_SECONDS


def test_the_winners_lease_is_on_disk_when_the_race_ends(cache_dir,
                                                         lease_path):
    _race([URL, URL], cache_dir)
    assert URL in json.loads(lease_path.read_text())


# --- file_lock --------------------------------------------------------------

def test_file_lock_yields_true_when_it_takes_the_lock(cache_dir):
    with guard.file_lock(cache_dir / "miso-copilot" / "_status.json") as held:
        assert held is True


def test_file_lock_creates_the_parent_directory(cache_dir):
    target = cache_dir / "made" / "up" / "_status.json"
    with guard.file_lock(target):
        pass
    assert target.parent.is_dir()


def test_file_lock_releases_the_lock_on_a_normal_exit(cache_dir, monkeypatch):
    target = cache_dir / "miso-copilot" / "_status.json"
    with guard.file_lock(target):
        pass
    monkeypatch.setattr(guard, "LOCK_TIMEOUT_SECONDS", 0)
    guard._unlock(guard._lock(target))


def test_file_lock_releases_the_lock_when_the_body_raises(
        cache_dir, monkeypatch):
    target = cache_dir / "miso-copilot" / "_status.json"
    with pytest.raises(ValueError):
        with guard.file_lock(target):
            raise ValueError("body blew up")
    monkeypatch.setattr(guard, "LOCK_TIMEOUT_SECONDS", 0)
    guard._unlock(guard._lock(target))


def test_file_lock_yields_false_when_the_lock_is_unavailable(
        cache_dir, monkeypatch, hold_lock):
    target = cache_dir / "miso-copilot" / "_status.json"
    monkeypatch.setattr(guard, "LOCK_TIMEOUT_SECONDS", 0)
    hold_lock(target)
    with guard.file_lock(target) as held:
        assert held is False


def test_file_lock_runs_the_body_anyway_when_the_lock_is_unavailable(
        cache_dir, monkeypatch, hold_lock):
    # Deliberate: losing the status file entirely is worse than an
    # interleaved write, which is what happened before this lock existed.
    target = cache_dir / "miso-copilot" / "_status.json"
    monkeypatch.setattr(guard, "LOCK_TIMEOUT_SECONDS", 0)
    hold_lock(target)
    ran = []
    with guard.file_lock(target):
        ran.append(True)
    assert ran == [True]


def test_file_lock_logs_a_warning_when_the_lock_is_unavailable(
        cache_dir, monkeypatch, hold_lock, caplog):
    target = cache_dir / "miso-copilot" / "_status.json"
    monkeypatch.setattr(guard, "LOCK_TIMEOUT_SECONDS", 0)
    hold_lock(target)
    with caplog.at_level(logging.WARNING, logger="backend.poller.guard"):
        with guard.file_lock(target):
            pass
    assert "proceeding unserialized" in caplog.text
