"""Everything in backend/poller/core.py except the validation gate.

The gate lives in test_validate.py. This file covers the config readers,
the atomic writes, the status merge, the prune, and poll_once end to end.

No test here reaches misoenergy.org. `fake_get` replaces requests.get with
a routing table and opens no socket; the handful of tests that want real
HTTP use the stub bound to 127.0.0.1. `cache_dir` and `poller_env` in
conftest.py are autouse, so XDG_CACHE_HOME and MISO_RAW_DIR are inside
tmp_path for every test in the file, including the ones that never look at
either.
"""

import json
import logging
import os
import tempfile
import threading
import time
import gzip
import zlib
from datetime import datetime
from pathlib import Path

import pytest
import requests
import urllib3

from backend.poller import core
from tests.support import GOOD_BODIES, GOOD_REF_IDS, FakeResponse

# A documentation range address. Not loopback, so the rate guard stays on,
# and not routable, so a request that escapes a patched requests.get dies
# in its own connect timeout rather than reaching anyone.
GUARDED_BASE = "http://198.51.100.1"

# Loopback, so poll_once bypasses the rate guard and writes no lease file.
# Port 9 is discard; nothing is ever sent there because requests.get is
# replaced in every test that uses this.
LOOPBACK_BASE = "http://127.0.0.1:9"

ALL_KEYS = ["FuelMix", "RealTimeTotalLoad", "Snapshot", "WindSolar"]


def wire_url(url: str) -> str:
    """The request target requests would actually put on the wire."""
    prepared = requests.PreparedRequest()
    prepared.prepare_url(url, None)
    return prepared.path_url


def entry_for(key: str, **overrides) -> dict:
    """A stored status entry for `key`, INITIAL_ENTRY plus overrides."""
    entry = dict(core.INITIAL_ENTRY)
    entry["path"] = next(e.path for e in core.ENDPOINTS if e.key == key)
    entry.update(overrides)
    return entry


def write_status(directory: Path, endpoints: dict) -> None:
    """Put a status file in place for a test that needs a prior cycle."""
    core.status_path(directory).write_text(
        json.dumps({"endpoints": endpoints}, indent=2))


# --- safe_base --------------------------------------------------------------

def test_safe_base_strips_userinfo():
    # This string reaches a WARNING every cycle and the status file.
    assert core.safe_base("http://user:pw@h.example/api") == \
        "http://h.example/api"


def test_safe_base_keeps_scheme_host_and_port():
    assert core.safe_base("https://user:pw@h.example:8443/") == \
        "https://h.example:8443"


def test_safe_base_keeps_a_base_with_no_userinfo_unchanged():
    assert core.safe_base("https://h.example") == "https://h.example"


def test_safe_base_returns_an_unparseable_base_verbatim():
    assert core.safe_base("not a url") == "not a url"


def test_safe_base_never_leaks_a_password_into_the_status_file(raw, good_get,
                                                               monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "http://user:sekrit@127.0.0.1:9")
    status = core.poll_once()
    assert "sekrit" not in json.dumps(status)


# --- base_url ---------------------------------------------------------------

def test_base_url_defaults_to_miso_when_unset(monkeypatch):
    monkeypatch.delenv("MISO_API_BASE", raising=False)
    assert core.base_url() == core.DEFAULT_BASE_URL


def test_base_url_strips_a_trailing_slash(monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "https://h.example/")
    assert core.base_url() == "https://h.example"


def test_base_url_strips_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "  https://h.example/  ")
    assert core.base_url() == "https://h.example"


def test_base_url_drops_a_fragment(monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "https://h.example/#frag")
    assert core.base_url() == "https://h.example"


def test_base_url_drops_a_query(monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "https://h.example/?a=b")
    assert core.base_url() == "https://h.example"


def test_base_url_drops_a_query_and_a_fragment_together(monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "https://h.example/?a=b#frag")
    assert core.base_url() == "https://h.example"


def test_base_url_keeps_a_path(monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "https://h.example/miso/")
    assert core.base_url() == "https://h.example/miso"


def test_base_url_lowercases_an_uppercase_scheme(monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "HTTPS://Host.Example/")
    assert core.base_url() == "https://Host.Example"


def test_base_url_keeps_userinfo_for_the_request(monkeypatch):
    # safe_base, not base_url, is what removes it before anything is shown.
    monkeypatch.setenv("MISO_API_BASE", "http://user:pw@h.example/")
    assert core.base_url() == "http://user:pw@h.example"


def test_base_url_returns_an_unparseable_string_untouched(monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "not a url")
    assert core.base_url() == "not a url"


def test_base_url_leaves_a_scheme_with_no_host_alone(monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "http://")
    assert core.base_url() == "http:"


def test_base_url_falls_back_to_the_default_when_empty(monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "")
    assert core.base_url() == core.DEFAULT_BASE_URL


@pytest.mark.parametrize("configured", [
    "https://h.example",
    "https://h.example/",
    "https://h.example/#frag",
    "https://h.example/?a=b",
    "https://h.example/?a=b#frag",
    "HTTPS://Host.Example/",
])
def test_normalized_base_sends_exactly_the_endpoint_path(monkeypatch,
                                                         configured):
    # The guard key and the URL on the wire have to be the same string, so
    # the normalized base joined to a path must survive requests untouched.
    monkeypatch.setenv("MISO_API_BASE", configured)
    base = core.base_url()
    for endpoint in core.ENDPOINTS:
        assert wire_url(base + endpoint.path) == endpoint.path


def test_a_fragment_in_the_base_would_collapse_four_links_into_one():
    # The bug base_url exists to close: four distinct guard keys, four
    # identical wire requests, the guard satisfied and one link hit 4x.
    raw_base = "https://h.example/#frag"
    keys = {raw_base + e.path for e in core.ENDPOINTS}
    sent = {wire_url(raw_base + e.path) for e in core.ENDPOINTS}
    assert len(keys) == 4
    assert len(sent) == 1


# --- raw_dir ----------------------------------------------------------------

def test_raw_dir_uses_the_override(raw):
    assert core.raw_dir() == raw


def test_raw_dir_resolves_a_relative_override(monkeypatch, tmp_path):
    # The status file records raw_dir as the absolute directory written; a
    # relative path recorded verbatim identifies nothing.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MISO_RAW_DIR", "relraw")
    assert core.raw_dir() == (tmp_path / "relraw").resolve()


def test_raw_dir_expands_a_tilde(monkeypatch):
    monkeypatch.setenv("MISO_RAW_DIR", "~/miso-raw-test")
    assert core.raw_dir() == (Path.home() / "miso-raw-test").resolve()


def test_raw_dir_defaults_to_the_repo_root_not_the_backend_package(
        monkeypatch):
    # Pure path arithmetic, no I/O: the repo's data/ is neither read nor
    # created here. The poller is one directory deeper than backend/
    # config.py, so copying that expression writes backend/data/raw.
    monkeypatch.delenv("MISO_RAW_DIR", raising=False)
    default = core.raw_dir()
    assert default.parts[-2:] == ("data", "raw")
    assert default.parent.parent.name != "backend"


# --- poll_seconds -----------------------------------------------------------

@pytest.mark.parametrize("configured,expected", [
    ("0", core.MIN_POLL_SECONDS),
    ("-1", core.MIN_POLL_SECONDS),
    ("4", core.MIN_POLL_SECONDS),
    ("5", 5),
    ("42", 42),
    (" 42 ", 42),
    ("3600", 3600),
    ("3601", core.MAX_POLL_SECONDS),
    ("abc", core.DEFAULT_POLL_SECONDS),
    ("1e9", core.DEFAULT_POLL_SECONDS),
    ("300.5", core.DEFAULT_POLL_SECONDS),
    ("", core.DEFAULT_POLL_SECONDS),
    ("   ", core.DEFAULT_POLL_SECONDS),
])
def test_poll_seconds_clamps_and_falls_back(monkeypatch, configured,
                                            expected):
    monkeypatch.setenv("MISO_POLL_SECONDS", configured)
    assert core.poll_seconds() == expected


def test_poll_seconds_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("MISO_POLL_SECONDS", raising=False)
    assert core.poll_seconds() == core.DEFAULT_POLL_SECONDS


def test_poll_seconds_warns_about_a_non_integer(monkeypatch, caplog):
    monkeypatch.setenv("MISO_POLL_SECONDS", "abc")
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core.poll_seconds()
    assert "not an integer" in caplog.text


# --- poller_enabled ---------------------------------------------------------

@pytest.mark.parametrize("configured", [
    "0", "false", "no", "off", "",
    "FALSE", "No", "OFF", "  false  ",
])
def test_poller_enabled_is_false_for_every_spelling_of_off(monkeypatch,
                                                           configured):
    # MISO_POLLER_ENABLED=false meaning "on" would be a trap.
    monkeypatch.setenv("MISO_POLLER_ENABLED", configured)
    assert core.poller_enabled() is False


@pytest.mark.parametrize("configured", ["1", "true", "yes", "on", "TRUE",
                                        "anything"])
def test_poller_enabled_is_true_for_anything_else(monkeypatch, configured):
    monkeypatch.setenv("MISO_POLLER_ENABLED", configured)
    assert core.poller_enabled() is True


def test_poller_enabled_defaults_to_true_when_unset(monkeypatch):
    monkeypatch.delenv("MISO_POLLER_ENABLED", raising=False)
    assert core.poller_enabled() is True


# --- base_is_loopback -------------------------------------------------------

@pytest.mark.parametrize("base", [
    "https://public-api.misoenergy.org",
    "http://public-api.misoenergy.org",
    "https://Public-API.MisoEnergy.ORG",
    "https://public-api.misoenergy.org:443",
    "https://public-api.misoenergy.org.",
    "https://public-api.misoenergy.org./api/FuelMix",
])
def test_no_spelling_of_miso_bypasses_the_rate_guard(base):
    # Comparing the base string to DEFAULT_BASE_URL answered the wrong
    # question: each of these is a different string and all of them reach
    # an organization that IP-bans.
    assert core.base_is_loopback(base) is False


@pytest.mark.parametrize("base", [
    "http://127.0.0.1",
    "http://127.0.0.1:8971",
    "http://127.0.0.1./api",
    "http://127.9.9.9",
    "http://localhost",
    "http://localhost:8971",
    "http://LocalHost:8971",
    "http://localhost.",
    "http://[::1]:8971",
])
def test_loopback_bypasses_the_rate_guard(base):
    assert core.base_is_loopback(base) is True


@pytest.mark.parametrize("base", [
    "http://0.0.0.0:8971",
    "http://192.168.1.10:8971",
    "http://10.0.0.5",
    "http://stub-box:8971",
    "http://[2001:db8::1]:8971",
    "not a url",
    "",
])
def test_anything_not_provably_loopback_stays_guarded(base):
    # Fail safe: a hostname nobody anticipated is guarded, not bypassed.
    assert core.base_is_loopback(base) is False


# --- now --------------------------------------------------------------------

def test_now_is_offset_aware():
    assert core.now().utcoffset() is not None


def test_now_uses_the_teams_local_zone():
    assert core.now().tzinfo is core.TIMEZONE


# --- write_atomic -----------------------------------------------------------

def test_write_atomic_writes_the_bytes(tmp_path):
    target = tmp_path / "payload.json"
    core.write_atomic(target, b'{"a": 1}')
    assert target.read_bytes() == b'{"a": 1}'


def test_write_atomic_leaves_the_file_world_readable(tmp_path):
    # mkstemp creates 0600; these files are a cross-lane interface.
    target = tmp_path / "payload.json"
    core.write_atomic(target, b"x")
    assert oct(target.stat().st_mode & 0o777) == "0o644"


def test_write_atomic_creates_missing_parents(tmp_path):
    target = tmp_path / "deep" / "deeper" / "payload.json"
    core.write_atomic(target, b"x")
    assert target.read_bytes() == b"x"


def test_write_atomic_leaves_no_tmp_behind_on_success(tmp_path):
    core.write_atomic(tmp_path / "payload.json", b"x")
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_atomic_overwrites_an_existing_file(tmp_path):
    target = tmp_path / "payload.json"
    target.write_bytes(b"old")
    core.write_atomic(target, b"new")
    assert target.read_bytes() == b"new"


def test_write_atomic_leaves_no_tmp_behind_when_fdopen_raises(tmp_path,
                                                              monkeypatch):
    def boom(fd, mode):
        # Do not close fd here: write_atomic owns it until fdopen returns,
        # and closing it twice is the bug the except clause exists to avoid.
        raise MemoryError("no memory for a buffer")

    monkeypatch.setattr(core.os, "fdopen", boom)
    with pytest.raises(MemoryError):
        core.write_atomic(tmp_path / "payload.json", b"x")
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_atomic_leaves_no_tmp_behind_when_replace_raises(tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(core.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        core.write_atomic(tmp_path / "payload.json", b"x")
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_atomic_does_not_damage_an_existing_file_when_it_fails(
        tmp_path, monkeypatch):
    target = tmp_path / "payload.json"
    target.write_bytes(b"good data")
    monkeypatch.setattr(core.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        core.write_atomic(target, b"truncated")
    assert target.read_bytes() == b"good data"


def test_write_atomic_survives_its_tmp_vanishing_before_the_cleanup(
        tmp_path, monkeypatch):
    # suppress rather than check-then-act: a temp file that disappears in
    # the finally block must not mask the exception that sent us there.
    def replace_then_remove(src, dst):
        os.unlink(src)
        raise OSError("replace failed after the temp file went away")

    monkeypatch.setattr(core.os, "replace", replace_then_remove)
    with pytest.raises(OSError, match="replace failed"):
        core.write_atomic(tmp_path / "payload.json", b"x")


def test_write_atomic_uses_a_unique_temp_name_per_writer(tmp_path,
                                                         monkeypatch):
    # A fixed "<file>.tmp" is not concurrency safe: two writers open the
    # same path, one truncates the other, and os.replace publishes a
    # partial file as a complete one.
    seen = []
    real_mkstemp = tempfile.mkstemp

    def recording(**kwargs):
        fd, name = real_mkstemp(**kwargs)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(core.tempfile, "mkstemp", recording)
    target = tmp_path / "payload.json"
    core.write_atomic(target, b"one")
    core.write_atomic(target, b"two")
    assert len(set(seen)) == 2
    assert str(target) + ".tmp" not in seen


def test_concurrent_writers_publish_one_whole_file_and_no_leftovers(tmp_path):
    target = tmp_path / "payload.json"
    bodies = [bytes([i]) * 20_000 for i in range(1, 9)]
    threads = [threading.Thread(target=core.write_atomic,
                                args=(target, body)) for body in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert target.read_bytes() in bodies
    assert list(tmp_path.glob("*.tmp")) == []


# --- sweep_stale_tmp --------------------------------------------------------

def age(path: Path, seconds: int) -> None:
    """Backdate `path` by `seconds`, so the sweep sees it as old."""
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_stale_tmp_seconds_is_ten_minutes():
    # A literal, because every other test in this section backdates by
    # core.STALE_TMP_SECONDS +/- 60 and so passes for any value of it. The
    # window has to be longer than the slowest cycle - otherwise the sweep
    # deletes a temp file a concurrent writer is still using and breaks its
    # os.replace - and short enough that a crashed run does not leave
    # litter around for a day.
    assert core.STALE_TMP_SECONDS == 600


def test_sweep_deletes_a_tmp_older_than_ten_minutes(tmp_path):
    stale = tmp_path / "FuelMix.abc.tmp"
    stale.write_bytes(b"x")
    age(stale, 601)
    core.sweep_stale_tmp(tmp_path)
    assert not stale.exists()


@pytest.fixture
def frozen_sweep_clock(monkeypatch):
    """Freeze core.time.time, and return the instant it is frozen at.

    sweep_stale_tmp recomputes `time.time() - STALE_TMP_SECONDS` internally,
    a moment after a test backdates a file, so the exact boundary is
    unreachable while the clock is still moving: a file aged to precisely
    600 seconds is already 600-and-a-bit by the time the cutoff is taken.
    Freezing makes both boundary cases deterministic, the way test_guard.py
    freezes guard._now to pin the lease boundaries to the second.
    """
    frozen = 1_800_000_000.0
    monkeypatch.setattr(core.time, "time", lambda: frozen)
    return frozen


def test_sweep_keeps_a_tmp_exactly_at_the_cutoff(tmp_path, frozen_sweep_clock):
    # The comparison is `<`, so a file of exactly the window's age survives.
    # Neither this case nor the one below is reachable by the +/- 60 tests.
    edge = tmp_path / "FuelMix.edge.tmp"
    edge.write_bytes(b"x")
    at_cutoff = frozen_sweep_clock - 600
    os.utime(edge, (at_cutoff, at_cutoff))
    core.sweep_stale_tmp(tmp_path)
    assert edge.exists()


def test_sweep_deletes_a_tmp_one_second_older_than_the_cutoff(
        tmp_path, frozen_sweep_clock):
    past = tmp_path / "FuelMix.past.tmp"
    past.write_bytes(b"x")
    older = frozen_sweep_clock - 601
    os.utime(past, (older, older))
    core.sweep_stale_tmp(tmp_path)
    assert not past.exists()


def test_sweep_keeps_a_tmp_a_concurrent_writer_may_still_be_using(tmp_path):
    # An unconditional sweep would break that writer's os.replace.
    fresh = tmp_path / "FuelMix.def.tmp"
    fresh.write_bytes(b"x")
    age(fresh, core.STALE_TMP_SECONDS - 60)
    core.sweep_stale_tmp(tmp_path)
    assert fresh.exists()


def test_sweep_leaves_payload_files_alone(tmp_path):
    payload = tmp_path / "FuelMix.json"
    payload.write_bytes(b"{}")
    age(payload, core.STALE_TMP_SECONDS + 60)
    core.sweep_stale_tmp(tmp_path)
    assert payload.exists()


def test_sweep_logs_what_it_removed(tmp_path, caplog):
    stale = tmp_path / "FuelMix.abc.tmp"
    stale.write_bytes(b"x")
    age(stale, core.STALE_TMP_SECONDS + 60)
    with caplog.at_level(logging.INFO, logger=core.log.name):
        core.sweep_stale_tmp(tmp_path)
    assert "FuelMix.abc.tmp" in caplog.text


def test_sweep_ignores_a_tmp_it_cannot_unlink(tmp_path):
    # A directory named *.tmp raises OSError on unlink. The sweep is
    # best-effort housekeeping and must not abort the cycle over it.
    stubborn = tmp_path / "stuck.tmp"
    stubborn.mkdir()
    (stubborn / "child").write_bytes(b"x")
    age(stubborn, core.STALE_TMP_SECONDS + 60)
    core.sweep_stale_tmp(tmp_path)
    assert stubborn.exists()


def test_sweep_on_an_empty_directory_does_nothing(raw):
    core.sweep_stale_tmp(raw)
    assert list(raw.iterdir()) == []


# --- read_status ------------------------------------------------------------

def test_read_status_returns_empty_when_the_file_is_missing(tmp_path):
    assert core.read_status(tmp_path) == {}


@pytest.mark.parametrize("contents", [
    "{{{",
    "null",
    "[]",
    '"x"',
    "{}",
    '{"endpoints": []}',
    '{"endpoints": "FuelMix"}',
    '{"endpoints": null}',
])
def test_read_status_treats_every_unusable_file_as_empty(tmp_path, contents):
    core.status_path(tmp_path).write_text(contents)
    assert core.read_status(tmp_path) == {}


def test_read_status_survives_a_deeply_nested_file(tmp_path):
    # json raises RecursionError, not ValueError. Letting it escape killed
    # both --once and the --status screen meant to explain the corruption.
    core.status_path(tmp_path).write_text("[" * 200_000 + "]" * 200_000)
    assert core.read_status(tmp_path) == {}


def test_read_status_treats_an_unreadable_path_as_empty(tmp_path):
    core.status_path(tmp_path).mkdir()
    assert core.read_status(tmp_path) == {}


def test_read_status_warns_when_it_starts_fresh(tmp_path, caplog):
    core.status_path(tmp_path).write_text("[]")
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core.read_status(tmp_path)
    assert "not shaped as expected" in caplog.text


def test_read_status_warns_when_the_file_will_not_parse(tmp_path, caplog):
    core.status_path(tmp_path).write_text("{{{")
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core.read_status(tmp_path)
    assert "unreadable" in caplog.text


def test_read_status_returns_a_well_formed_file(tmp_path):
    write_status(tmp_path, {"FuelMix": entry_for("FuelMix")})
    assert core.read_status(tmp_path)["endpoints"]["FuelMix"]["path"] == \
        "/api/FuelMix"


def test_read_status_accepts_an_entry_that_is_not_an_object(tmp_path):
    # The endpoints object is well shaped; a junk entry inside it is
    # _usable_entry's problem, not read_status's.
    write_status(tmp_path, {"FuelMix": 3})
    assert core.read_status(tmp_path)["endpoints"]["FuelMix"] == 3


def test_status_path_is_inside_the_raw_directory(tmp_path):
    assert core.status_path(tmp_path) == tmp_path / "_status.json"


# --- _entry_is_usable and _usable_entry -------------------------------------

@pytest.mark.parametrize("entry", [None, 3, "x", [], 3.5])
def test_an_entry_that_is_not_an_object_is_unusable(entry):
    assert core._entry_is_usable(entry) is False


@pytest.mark.parametrize("failures", [True, False, "3", -1, 1.0, None, []])
def test_a_bad_consecutive_failures_makes_an_entry_unusable(failures):
    # bool before int: True is an instance of int, so a hand-edited
    # "consecutive_failures": true once passed and counted up from 1.
    assert core._entry_is_usable({"consecutive_failures": failures}) is False


def test_a_zero_consecutive_failures_is_usable():
    assert core._entry_is_usable({"consecutive_failures": 0}) is True


@pytest.mark.parametrize("field", ["last_attempt", "last_success",
                                   "ref_id_changed_at"])
def test_a_non_string_timestamp_makes_an_entry_unusable(field):
    entry = {"consecutive_failures": 0, field: 1234567890}
    assert core._entry_is_usable(entry) is False


@pytest.mark.parametrize("field", ["last_attempt", "last_success",
                                   "ref_id_changed_at"])
def test_an_unparseable_timestamp_makes_an_entry_unusable(field):
    entry = {"consecutive_failures": 0, field: "yesterday"}
    assert core._entry_is_usable(entry) is False


def test_null_timestamps_are_usable():
    entry = {"consecutive_failures": 2, "last_attempt": None,
             "last_success": None, "ref_id_changed_at": None}
    assert core._entry_is_usable(entry) is True


def test_iso_timestamps_are_usable():
    stamp = core.now().isoformat()
    entry = {"consecutive_failures": 2, "last_attempt": stamp,
             "last_success": stamp, "ref_id_changed_at": stamp}
    assert core._entry_is_usable(entry) is True


def test_a_bad_entry_is_reinitialized_not_merged():
    assert core._usable_entry({"consecutive_failures": "3"}) == \
        core.INITIAL_ENTRY


def test_a_usable_entry_keeps_its_carried_forward_fields():
    stored = entry_for("FuelMix", consecutive_failures=7, ref_id="r-1",
                       last_success="1970-01-01T00:00:00-05:00")
    merged = core._usable_entry(stored)
    assert merged["consecutive_failures"] == 7
    assert merged["ref_id"] == "r-1"


def test_a_usable_entry_drops_keys_that_are_not_in_the_shape():
    # `outcome` in particular must never be inherited: it describes one
    # cycle, so a stale value cannot be allowed to survive a merge.
    stored = entry_for("FuelMix", outcome="ok", junk="x")
    merged = core._usable_entry(stored)
    assert "outcome" not in merged
    assert "junk" not in merged


def test_a_usable_entry_is_never_the_callers_object():
    stored = entry_for("FuelMix", consecutive_failures=1)
    merged = core._usable_entry(stored)
    merged["consecutive_failures"] = 99
    assert stored["consecutive_failures"] == 1


def test_an_unusable_entry_is_never_the_shared_initial_entry():
    merged = core._usable_entry("junk")
    merged["consecutive_failures"] = 99
    assert core.INITIAL_ENTRY["consecutive_failures"] == 0


# --- _merge_observation -----------------------------------------------------

def test_a_skip_records_only_the_outcome():
    entry = dict(core.INITIAL_ENTRY, consecutive_failures=5,
                 last_error="HTTP 503", last_attempt="1970-01-01T00:00:00")
    core._merge_observation(entry, core._skipped_observation())
    assert entry["outcome"] == "skipped"
    assert entry["consecutive_failures"] == 5


def test_a_skip_does_not_overwrite_the_last_real_error():
    # An endpoint that failed five times with 503 and is then skipped must
    # still report the 503 - that is the incident being debugged.
    entry = dict(core.INITIAL_ENTRY, last_error="HTTP 503")
    core._merge_observation(entry, core._skipped_observation())
    assert entry["last_error"] == "HTTP 503"


def test_a_skip_does_not_move_last_attempt():
    entry = dict(core.INITIAL_ENTRY, last_attempt="1970-01-01T00:00:00")
    core._merge_observation(entry, core._skipped_observation())
    assert entry["last_attempt"] == "1970-01-01T00:00:00"


def test_a_failure_increments_consecutive_failures():
    entry = dict(core.INITIAL_ENTRY, consecutive_failures=2)
    observed = {"outcome": "failed", "last_attempt": "1970-01-01T00:00:00",
                "http_status": 503, "bytes": 217,
                "last_error": "HTTP 503", "ref_id": None}
    core._merge_observation(entry, observed)
    assert entry["consecutive_failures"] == 3


def test_a_failure_carries_forward_last_success_and_ref_id():
    entry = dict(core.INITIAL_ENTRY, last_success="1970-01-01T00:00:00",
                 ref_id="r-1", ref_id_changed_at="1970-01-01T00:00:00")
    observed = {"outcome": "failed", "last_attempt": "1970-01-02T00:00:00",
                "http_status": 503, "bytes": 217,
                "last_error": "HTTP 503", "ref_id": None}
    core._merge_observation(entry, observed)
    assert entry["last_success"] == "1970-01-01T00:00:00"
    assert entry["ref_id"] == "r-1"
    assert entry["ref_id_changed_at"] == "1970-01-01T00:00:00"


def test_a_failure_records_bytes_for_a_response_that_did_arrive(tmp_path):
    # Spec 6.2: a 503 carries bytes, not null. null is for transport
    # failures where no response arrived at all.
    entry = dict(core.INITIAL_ENTRY)
    observed = {"outcome": "failed", "last_attempt": "1970-01-01T00:00:00",
                "http_status": 503, "bytes": 217,
                "last_error": "HTTP 503", "ref_id": None}
    core._merge_observation(entry, observed)
    assert (entry["http_status"], entry["bytes"]) == (503, 217)


def test_a_success_resets_consecutive_failures():
    entry = dict(core.INITIAL_ENTRY, consecutive_failures=9)
    observed = {"outcome": "ok", "last_attempt": "1970-01-01T00:00:00",
                "http_status": 200, "bytes": 100,
                "last_error": None, "ref_id": "r-1"}
    core._merge_observation(entry, observed)
    assert entry["consecutive_failures"] == 0
    assert entry["last_error"] is None


def test_a_success_sets_last_success_to_this_attempt():
    entry = dict(core.INITIAL_ENTRY)
    observed = {"outcome": "ok", "last_attempt": "1970-01-01T00:00:00",
                "http_status": 200, "bytes": 100,
                "last_error": None, "ref_id": "r-1"}
    core._merge_observation(entry, observed)
    assert entry["last_success"] == "1970-01-01T00:00:00"


def test_the_first_success_sets_ref_id_changed_at():
    # Stored None differs from a new string, so they differ and the field
    # is set. Spec 6.4 calls this out explicitly.
    entry = dict(core.INITIAL_ENTRY)
    observed = {"outcome": "ok", "last_attempt": "1970-01-01T00:00:00",
                "http_status": 200, "bytes": 100,
                "last_error": None, "ref_id": "r-1"}
    core._merge_observation(entry, observed)
    assert entry["ref_id_changed_at"] == "1970-01-01T00:00:00"


def test_an_unchanged_ref_id_leaves_ref_id_changed_at_alone():
    # The frozen-feed signal: last_success advances, this one does not.
    entry = dict(core.INITIAL_ENTRY, ref_id="r-1",
                 ref_id_changed_at="1970-01-01T00:00:00")
    observed = {"outcome": "ok", "last_attempt": "1970-01-02T00:00:00",
                "http_status": 200, "bytes": 100,
                "last_error": None, "ref_id": "r-1"}
    core._merge_observation(entry, observed)
    assert entry["ref_id_changed_at"] == "1970-01-01T00:00:00"
    assert entry["last_success"] == "1970-01-02T00:00:00"


def test_a_changed_ref_id_moves_ref_id_changed_at():
    entry = dict(core.INITIAL_ENTRY, ref_id="r-1",
                 ref_id_changed_at="1970-01-01T00:00:00")
    observed = {"outcome": "ok", "last_attempt": "1970-01-02T00:00:00",
                "http_status": 200, "bytes": 100,
                "last_error": None, "ref_id": "r-2"}
    core._merge_observation(entry, observed)
    assert entry["ref_id_changed_at"] == "1970-01-02T00:00:00"


def test_snapshots_none_ref_id_never_moves_ref_id_changed_at():
    # Snapshot has no RefId, so stored and observed are both None and the
    # field stays null forever (spec 6.4).
    entry = dict(core.INITIAL_ENTRY, ref_id=None, ref_id_changed_at=None)
    observed = {"outcome": "ok", "last_attempt": "1970-01-02T00:00:00",
                "http_status": 200, "bytes": 100,
                "last_error": None, "ref_id": None}
    core._merge_observation(entry, observed)
    assert entry["ref_id_changed_at"] is None


def test_an_internal_error_observation_is_a_failure():
    observed = core._internal_error_observation()
    assert observed["outcome"] == "failed"
    assert observed["last_error"] == "internal error"
    assert observed["last_attempt"] is not None


# --- _prune_payloads --------------------------------------------------------

def test_a_departed_endpoints_payload_is_deleted(tmp_path):
    (tmp_path / "Departed.json").write_bytes(b"{}")
    core._prune_payloads(tmp_path, {"Departed": {}}, {"FuelMix": {}})
    assert not (tmp_path / "Departed.json").exists()


def test_a_departed_endpoint_leaves_the_status_file_pruned(raw, good_get,
                                                            monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    write_status(raw, {"Departed": entry_for("FuelMix")})
    status = core.poll_once()
    assert set(status["endpoints"]) == set(ALL_KEYS)


def test_a_live_endpoints_payload_survives_a_key_differing_only_in_case(
        tmp_path):
    # A real data-loss bug. On a case-insensitive filesystem a stale entry
    # keyed "Windsolar" names the same file as the live "WindSolar", so a
    # case-sensitive test deleted the payload this cycle just wrote.
    (tmp_path / "WindSolar.json").write_bytes(b'{"live": true}')
    core._prune_payloads(tmp_path, {"Windsolar": {}}, {"WindSolar": {}})
    assert (tmp_path / "WindSolar.json").read_bytes() == b'{"live": true}'


def test_a_live_endpoint_is_not_pruned(tmp_path):
    (tmp_path / "FuelMix.json").write_bytes(b"{}")
    core._prune_payloads(tmp_path, {"FuelMix": {}}, {"FuelMix": {}})
    assert (tmp_path / "FuelMix.json").exists()


@pytest.mark.parametrize("key", ["_status", "_STATUS", "_Status"])
def test_no_casing_of_the_status_key_deletes_the_status_file(tmp_path, key):
    # A Path equality test is case-sensitive wherever it runs, so on APFS
    # "_STATUS" named a different Path and unlinked the real status file.
    status = core.status_path(tmp_path)
    status.write_text("{}")
    core._prune_payloads(tmp_path, {key: {}}, {"FuelMix": {}})
    assert status.exists()


@pytest.mark.parametrize("key", ["..", "../x", "/etc/passwd", ".hidden",
                                 "a" * 65, "", "with space", "a.b"])
def test_an_unsafe_key_deletes_nothing(tmp_path, key):
    # The status file is hand-editable, so a key is not trusted to be a
    # safe path component just because it was in there.
    before = sorted(p.name for p in tmp_path.iterdir())
    core._prune_payloads(tmp_path, {key: {}}, {"FuelMix": {}})
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.parametrize("key", [".hidden", "a" * 65, "with space", "a.b",
                                 "FuelMix.bak"])
def test_an_unsafe_key_does_not_delete_the_file_it_names(tmp_path, key):
    # The test above starts from an empty directory, so it holds even if
    # _prune_payloads did nothing at all. This one puts the file the key
    # names on disk first, so the assertion has something to protect.
    victim = tmp_path / f"{key}.json"
    victim.write_bytes(b"{}")
    core._prune_payloads(tmp_path, {key: {}}, {"FuelMix": {}})
    assert victim.exists()


@pytest.mark.parametrize("key", ["FuelMix/../outside", "sub/../../outside",
                                 "FuelMix.bak", "Fuel Mix"])
def test_a_key_safe_only_at_its_start_deletes_nothing(tmp_path, key):
    # SAFE_KEY.fullmatch, not .match. Every key here begins with characters
    # the pattern accepts and then leaves the safe set, so an unanchored
    # match admits them - and the first two escape the directory entirely.
    # The sub/ directory is what makes this bite: without an existing path
    # component the unlink would fail with FileNotFoundError for the wrong
    # reason and the test would pass against a broken gate.
    (tmp_path / "sub").mkdir()
    outside = tmp_path.parent / "outside.json"
    outside.write_bytes(b"{}")
    inside = tmp_path / f"{key}.json"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"{}")
    core._prune_payloads(tmp_path, {key: {}}, {"FuelMix": {}})
    assert outside.exists()
    assert inside.exists()


@pytest.mark.parametrize("key", [3, None, ("FuelMix",)])
def test_a_non_string_key_deletes_nothing(tmp_path, key):
    (tmp_path / "FuelMix.json").write_bytes(b"{}")
    core._prune_payloads(tmp_path, {key: {}}, {"FuelMix": {}})
    assert (tmp_path / "FuelMix.json").exists()


def test_an_unsafe_key_is_logged(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core._prune_payloads(tmp_path, {"../x": {}}, {"FuelMix": {}})
    assert "unrecognized endpoint key" in caplog.text


def test_a_traversing_key_does_not_reach_outside_the_directory(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}")
    inner = tmp_path / "inner"
    inner.mkdir()
    core._prune_payloads(inner, {"../outside": {}}, {"FuelMix": {}})
    assert outside.exists()


def test_pruning_a_symlink_removes_the_link_and_not_its_target(tmp_path):
    target = tmp_path / "precious.json"
    target.write_bytes(b'{"precious": true}')
    directory = tmp_path / "inner"
    directory.mkdir()
    link = directory / "Departed.json"
    link.symlink_to(target)
    core._prune_payloads(directory, {"Departed": {}}, {"FuelMix": {}})
    assert not link.exists()
    assert not link.is_symlink()
    assert target.read_bytes() == b'{"precious": true}'


def test_pruning_an_endpoint_with_no_payload_file_is_quiet(raw):
    core._prune_payloads(raw, {"Departed": {}}, {"FuelMix": {}})
    assert list(raw.iterdir()) == []


def test_pruning_logs_but_survives_a_file_it_cannot_delete(tmp_path, caplog):
    stubborn = tmp_path / "Departed.json"
    stubborn.mkdir()
    (stubborn / "child").write_bytes(b"x")
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core._prune_payloads(tmp_path, {"Departed": {}}, {"FuelMix": {}})
    assert "could not delete" in caplog.text
    assert stubborn.exists()


def test_pruning_logs_what_it_deleted(tmp_path, caplog):
    (tmp_path / "Departed.json").write_bytes(b"{}")
    with caplog.at_level(logging.INFO, logger=core.log.name):
        core._prune_payloads(tmp_path, {"Departed": {}}, {"FuelMix": {}})
    assert "pruned endpoint Departed" in caplog.text


# --- _log_configuration -----------------------------------------------------

def test_the_default_base_is_not_announced(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("MISO_RAW_DIR", raising=False)
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core._log_configuration(core.DEFAULT_BASE_URL, False, tmp_path)
    assert caplog.text == ""


def test_a_loopback_base_announces_that_the_guard_is_bypassed(tmp_path,
                                                              caplog):
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core._log_configuration("http://127.0.0.1:8971", True, tmp_path)
    assert "RATE GUARD BYPASSED" in caplog.text


def test_a_non_loopback_override_announces_that_the_guard_is_active(tmp_path,
                                                                    caplog):
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core._log_configuration("https://mirror.example", False, tmp_path)
    assert "rate guard ACTIVE" in caplog.text


def test_the_configuration_log_never_carries_a_password(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core._log_configuration("http://user:sekrit@mirror.example", False,
                                tmp_path)
    assert "sekrit" not in caplog.text


def test_a_raw_dir_override_is_announced(raw, caplog):
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core._log_configuration(core.DEFAULT_BASE_URL, False, raw)
    assert "MISO_RAW_DIR is set" in caplog.text


# --- _fetch -----------------------------------------------------------------

def fetch(fake, value, key="FuelMix"):
    """Route one endpoint to `value` and fetch it."""
    endpoint = next(e for e in core.ENDPOINTS if e.key == key)
    fake.set(endpoint.path, value)
    return core._fetch(endpoint, "http://127.0.0.1:9" + endpoint.path)


@pytest.mark.parametrize("exception,expected", [
    (requests.exceptions.MissingSchema("no scheme"), "bad base url"),
    (requests.exceptions.InvalidURL("bad url"), "bad base url"),
    (requests.exceptions.Timeout("timed out"), "timeout"),
    (requests.exceptions.ConnectTimeout("timed out"), "timeout"),
    (requests.exceptions.ReadTimeout("timed out"), "timeout"),
    (requests.exceptions.ChunkedEncodingError("cut off"),
     "truncated response"),
    (requests.exceptions.ContentDecodingError("bad gzip"),
     "truncated response"),
    (requests.exceptions.ConnectionError("refused"), "connection error"),
    (requests.exceptions.TooManyRedirects("loop"), "internal error"),
])
def test_every_transport_failure_maps_to_its_closed_vocabulary_string(
        fake_get, exception, expected):
    # Never str(exception): requests exceptions are multi-line and embed
    # the full URL, and this string lands in a file the RAG lane renders.
    result = fetch(fake_get, exception)
    assert result["error"] == expected
    assert result["ok"] is False


def test_a_transport_failure_records_no_status_and_no_bytes(fake_get):
    # null is reserved for the case where no response arrived at all.
    result = fetch(fake_get, requests.exceptions.ConnectionError("refused"))
    assert result["http_status"] is None
    assert result["bytes"] is None


def test_an_unforeseen_request_exception_is_logged_as_our_bug(fake_get,
                                                              caplog):
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        fetch(fake_get, requests.exceptions.TooManyRedirects("loop"))
    assert "unexpected request failure" in caplog.text


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_a_failure_and_is_not_followed(fake_get, status):
    # A moved endpoint is news, not something to follow.
    result = fetch(fake_get, FakeResponse(b"", status))
    assert result["error"] == f"redirect {status}"


@pytest.mark.parametrize("status", [400, 403, 429, 500, 503])
def test_a_non_200_records_its_code(fake_get, status):
    result = fetch(fake_get, FakeResponse(b"error body", status))
    assert result["error"] == f"HTTP {status}"


def test_a_non_200_still_records_the_bytes_it_received(fake_get):
    result = fetch(fake_get, FakeResponse(b"error body", 503))
    assert (result["http_status"], result["bytes"]) == (503, len(b"error body"))


def test_a_200_that_fails_the_gate_is_a_failure(fake_get):
    result = fetch(fake_get, FakeResponse(b"<html>", 200))
    assert result["error"] == "invalid JSON"


def test_a_200_that_passes_the_gate_is_a_success(fake_get):
    result = fetch(fake_get, FakeResponse(GOOD_BODIES["/api/FuelMix"], 200))
    assert result["ok"] is True
    assert result["ref_id"] == "ref-fuelmix"


def test_fetch_does_not_follow_redirects(fake_get):
    fetch(fake_get, FakeResponse(GOOD_BODIES["/api/FuelMix"], 200))
    assert fake_get.calls[0][1]["allow_redirects"] is False


def test_fetch_sends_the_projects_user_agent(fake_get):
    fetch(fake_get, FakeResponse(GOOD_BODIES["/api/FuelMix"], 200))
    assert "miso-copilot" in fake_get.calls[0][1]["headers"]["User-Agent"]


# --- the body: bounded in size, in time, and in encoding ---------------------
#
# Every test below exists because a stress run found the unbounded version.
# core reads the body with raw.read1 rather than response.content precisely
# so that these three limits can exist at all.

def test_max_response_bytes_is_thirty_two_mebibytes():
    # A literal. The largest real payload is about 12 KB, so this is a wide
    # margin that is still a margin. Measured before the cap existed: a
    # 256 MB reply cost 582 MB of resident memory in a process that also
    # serves /ask.
    assert core.MAX_RESPONSE_BYTES == 32 * 1024 * 1024


def test_max_request_seconds_is_twenty():
    # A literal, for the same reason. requests' read timeout is per socket
    # read, so without this a reply that keeps dripping never ends.
    assert core.MAX_REQUEST_SECONDS == 20


def test_a_reply_over_the_size_cap_is_refused(fake_get):
    oversized = b"x" * (core.MAX_RESPONSE_BYTES + 1)
    observed = fetch(fake_get, FakeResponse(oversized, 200, pieces=64))
    assert observed["ok"] is False
    assert observed["error"] == "response too large"


def test_a_reply_at_the_size_cap_is_still_read(fake_get):
    # The comparison is `>`, so exactly the cap is allowed through. It fails
    # the JSON gate rather than the size gate, which is the point: the size
    # check is what we are pinning, not the parser.
    at_cap = b"x" * core.MAX_RESPONSE_BYTES
    observed = fetch(fake_get, FakeResponse(at_cap, 200, pieces=64))
    assert observed["error"] == "invalid JSON"


def test_a_refused_reply_reports_no_byte_count(fake_get):
    # bytes stays None: nothing was kept, and a partial count would read as
    # though that many bytes had been accepted and stored.
    observed = fetch(fake_get,
                     FakeResponse(b"x" * (core.MAX_RESPONSE_BYTES + 1), 200,
                                  pieces=8))
    assert observed["bytes"] is None


def test_a_reply_that_arrives_too_slowly_is_a_timeout(fake_get, monkeypatch):
    # The deadline is wall clock, so the clock is moved rather than waited on.
    ticks = iter([0.0] + [core.MAX_REQUEST_SECONDS + 1] * 50)
    monkeypatch.setattr(core.time, "monotonic", lambda: next(ticks))
    observed = fetch(fake_get,
                     FakeResponse(GOOD_BODIES["/api/FuelMix"], 200, pieces=4))
    assert observed["ok"] is False
    assert observed["error"] == "timeout"


def test_a_prompt_reply_is_not_cut_off_by_the_deadline(fake_get):
    observed = fetch(fake_get,
                     FakeResponse(GOOD_BODIES["/api/FuelMix"], 200, pieces=8))
    assert observed["ok"] is True
    assert observed["ref_id"] == "ref-fuelmix"


def test_the_request_asks_only_for_encodings_this_lane_can_decode(fake_get):
    # read1 hands back undecoded bytes, so this lane decodes them itself and
    # may only ask for what _decoder_for handles. Asking for br here would
    # invite a reply nothing can read.
    fetch(fake_get, FakeResponse(GOOD_BODIES["/api/FuelMix"], 200))
    assert fake_get.calls[0][1]["headers"]["Accept-Encoding"] == "gzip, deflate"


@pytest.mark.parametrize("encoding,pack", [
    ("gzip", lambda b: gzip.compress(b)),
    ("GZIP", lambda b: gzip.compress(b)),
    ("deflate", lambda b: zlib.compress(b)),
])
def test_a_compressed_reply_is_decoded(fake_get, encoding, pack):
    # MISO serves these gzipped (spec 5.1), so this is the normal path in
    # production, not an edge case.
    observed = fetch(fake_get, FakeResponse(
        pack(GOOD_BODIES["/api/FuelMix"]), 200,
        headers={"Content-Encoding": encoding}))
    assert observed["ok"] is True
    assert observed["ref_id"] == "ref-fuelmix"


def test_a_compressed_reply_is_decoded_across_several_reads(fake_get):
    # read1 returns what has arrived, so the decoder is fed in pieces and
    # has to carry state between them.
    body = gzip.compress(GOOD_BODIES["/api/RealTimeTotalLoad"])
    observed = fetch(fake_get, FakeResponse(
        body, 200, headers={"Content-Encoding": "gzip"}, pieces=7),
        key="RealTimeTotalLoad")
    assert observed["ok"] is True
    assert observed["ref_id"] == "ref-load"


def test_the_payload_written_is_the_decoded_bytes(raw, fake_get, monkeypatch):
    # What lands in data/raw must be what curl would show, not gzip.
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    for path, body in GOOD_BODIES.items():
        fake_get.set(path, FakeResponse(gzip.compress(body), 200,
                                        headers={"Content-Encoding": "gzip"}))
    core.poll_once()
    assert (raw / "FuelMix.json").read_bytes() == GOOD_BODIES["/api/FuelMix"]


@pytest.mark.parametrize("encoding", ["br", "zstd", "compress", "weird"])
def test_an_encoding_this_lane_cannot_decode_is_named(fake_get, encoding):
    # Named rather than left to the parser: the bytes would be perfectly
    # valid something, and "invalid JSON" would send someone hunting a
    # parser bug that does not exist.
    observed = fetch(fake_get, FakeResponse(
        GOOD_BODIES["/api/FuelMix"], 200,
        headers={"Content-Encoding": encoding}))
    assert observed["error"] == "unexpected encoding"


def test_a_corrupt_gzip_body_is_a_truncated_response(fake_get):
    observed = fetch(fake_get, FakeResponse(
        b"\x1f\x8b\x08\x00 not actually gzip at all", 200,
        headers={"Content-Encoding": "gzip"}))
    assert observed["error"] == "truncated response"


def test_a_corrupted_compressed_body_is_a_truncated_response(fake_get):
    # A valid gzip header followed by garbage. zlib raises part way through,
    # which is a different path from the stream simply stopping early.
    good = gzip.compress(GOOD_BODIES["/api/FuelMix"])
    corrupt = good[:6] + bytes(b ^ 0xFF for b in good[6:])
    observed = fetch(fake_get, FakeResponse(
        corrupt, 200, headers={"Content-Encoding": "gzip"}))
    assert observed["error"] == "truncated response"


def test_a_decoder_that_fails_at_the_flush_is_a_truncated_response(
        fake_get, monkeypatch):
    # zlib can also fail when the final block is assembled rather than while
    # chunks are fed in. Same verdict, different moment.
    class FailingFlush:
        eof = True

        def decompress(self, data, max_length=None):
            return data

        def flush(self):
            raise zlib.error("incomplete final block")

    monkeypatch.setattr(core, "_decoder_for", lambda response: FailingFlush())
    observed = fetch(fake_get, FakeResponse(
        GOOD_BODIES["/api/FuelMix"], 200,
        headers={"Content-Encoding": "gzip"}))
    assert observed["error"] == "truncated response"


def test_a_compression_bomb_is_refused_before_it_is_allocated(fake_get):
    # A few hundred KB of zeros that expands past the cap. The decoder is
    # given an explicit max_length, so this never allocates the full size.
    bomb = gzip.compress(b"\0" * (core.MAX_RESPONSE_BYTES + 1024))
    assert len(bomb) < core.MAX_RESPONSE_BYTES      # arrives well under the cap
    observed = fetch(fake_get, FakeResponse(
        bomb, 200, headers={"Content-Encoding": "gzip"}, pieces=4))
    assert observed["error"] == "response too large"


@pytest.mark.parametrize("header", ["identity", "IDENTITY", "", None])
def test_an_uncompressed_reply_is_read_normally(fake_get, header):
    headers = {} if header is None else {"Content-Encoding": header}
    observed = fetch(fake_get, FakeResponse(GOOD_BODIES["/api/FuelMix"], 200,
                                            headers=headers))
    assert observed["ok"] is True


def test_the_response_is_closed_even_on_a_refusal(fake_get):
    # stream=True holds the connection until it is released. A refused reply
    # is the path most likely to leak one.
    response = FakeResponse(b"x" * (core.MAX_RESPONSE_BYTES + 1), 200,
                            pieces=8)
    fetch(fake_get, response)
    assert response.closed is True


@pytest.mark.parametrize("exception,expected", [
    (urllib3.exceptions.ReadTimeoutError(None, "u", "read timed out"),
     "timeout"),
    (urllib3.exceptions.ProtocolError("connection broken"),
     "truncated response"),
    (urllib3.exceptions.DecodeError("bad encoding"), "truncated response"),
    (urllib3.exceptions.IncompleteRead(0, 10), "truncated response"),
    (urllib3.exceptions.SSLError("handshake failed"), "internal error"),
])
def test_a_urllib3_failure_during_the_body_read_keeps_the_vocabulary(
        fake_get, exception, expected):
    # requests wraps urllib3's exceptions on the paths that go through
    # requests. read1 is not one of them, so these arrive unwrapped, and
    # without naming them a plain read timeout is reported as an internal
    # error - which blames us for MISO being slow.
    class Exploding(FakeResponse):
        def __init__(self):
            super().__init__(b"", 200)
            self.raw = self

        def read1(self, n=None):
            raise exception

    observed = fetch(fake_get, Exploding())
    assert observed["error"] == expected


def test_fetch_bounds_both_the_connect_and_the_read(fake_get):
    # schedule.DaemonThreadExecutor's whole justification is that a cycle is
    # bounded - "four links at a 15 second read timeout is up to 80 seconds".
    # Without a timeout a hung connection blocks the cycle forever, and
    # max_instances=1 then stops polling permanently with nothing in the log.
    fetch(fake_get, FakeResponse(GOOD_BODIES["/api/FuelMix"], 200))
    assert fake_get.calls[0][1]["timeout"] == core.TIMEOUT


def test_the_timeout_bounds_the_connect_and_the_read_separately():
    # A literal, not a re-derivation of core.TIMEOUT: a test that reads the
    # constant it is pinning passes for every value of it.
    assert core.TIMEOUT == (5, 15)


@pytest.mark.parametrize("status", [100, 199, 201, 202, 203, 204, 206])
def test_a_2xx_that_is_not_200_is_a_failure(fake_get, status):
    # The gate is `status != 200`, not `status >= 400`. A 206 Partial Content
    # carries a truncated body that still parses as JSON and still matches the
    # shape, so a >= 400 gate would write it to data/raw and record it "ok" -
    # publishing a half payload to the RAG lane as authoritative.
    observed = fetch(fake_get,
                     FakeResponse(GOOD_BODIES["/api/FuelMix"], status))
    assert observed["ok"] is False
    assert observed["error"] == f"HTTP {status}"


def test_a_206_partial_body_is_never_written(raw, fake_get, monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", "http://127.0.0.1:9")
    for path, body in GOOD_BODIES.items():
        fake_get.set(path, FakeResponse(body, 206))
    core.poll_once()
    assert list(raw.glob("*.json")) == [core.status_path(raw)]


# --- _poll_endpoint ---------------------------------------------------------

def test_a_guarded_endpoint_that_cannot_claim_is_skipped(tmp_path,
                                                         monkeypatch):
    monkeypatch.setattr(core.guard, "claim", lambda url: False)
    observed = core._poll_endpoint(core.ENDPOINTS[0], GUARDED_BASE,
                                   tmp_path, bypass_guard=False)
    # The literal, not core._skipped_observation(): comparing the function's
    # output to the same function asserts only that it is deterministic. In
    # particular last_error must stay None so _merge_observation leaves the
    # error the endpoint already had in place - a skip is not a failure.
    assert observed == {
        "outcome": "skipped",
        "last_attempt": None,
        "http_status": None,
        "bytes": None,
        "last_error": None,
        "ref_id": None,
    }


def test_a_skip_writes_no_payload_file(raw, monkeypatch):
    monkeypatch.setattr(core.guard, "claim", lambda url: False)
    core._poll_endpoint(core.ENDPOINTS[0], GUARDED_BASE, raw,
                        bypass_guard=False)
    assert list(raw.iterdir()) == []


def test_a_guarded_endpoint_that_can_claim_is_fetched(tmp_path, fake_get,
                                                      monkeypatch):
    claimed = []
    monkeypatch.setattr(core.guard, "claim", lambda url: claimed.append(url)
                        or True)
    fake_get.serve_good()
    endpoint = core.ENDPOINTS[0]
    observed = core._poll_endpoint(endpoint, LOOPBACK_BASE + endpoint.path,
                                   tmp_path, bypass_guard=False)
    assert claimed == [LOOPBACK_BASE + endpoint.path]
    assert observed["outcome"] == "ok"


def test_a_bypassed_endpoint_never_claims_a_lease(tmp_path, fake_get,
                                                  monkeypatch):
    def refuse(url):
        raise AssertionError("the guard must not be consulted on loopback")

    monkeypatch.setattr(core.guard, "claim", refuse)
    fake_get.serve_good()
    endpoint = core.ENDPOINTS[0]
    observed = core._poll_endpoint(endpoint, LOOPBACK_BASE + endpoint.path,
                                   tmp_path, bypass_guard=True)
    assert observed["outcome"] == "ok"


def test_a_successful_endpoint_writes_its_payload_verbatim(tmp_path,
                                                           good_get):
    endpoint = core.ENDPOINTS[0]
    core._poll_endpoint(endpoint, LOOPBACK_BASE + endpoint.path, tmp_path,
                        bypass_guard=True)
    assert (tmp_path / "FuelMix.json").read_bytes() == \
        GOOD_BODIES["/api/FuelMix"]


def test_a_failed_endpoint_leaves_its_existing_payload_untouched(tmp_path,
                                                                 fake_get):
    endpoint = core.ENDPOINTS[0]
    payload = tmp_path / "FuelMix.json"
    payload.write_bytes(b'{"old": true}')
    fake_get.set(endpoint.path, FakeResponse(b"<html>", 200))
    observed = core._poll_endpoint(endpoint, LOOPBACK_BASE + endpoint.path,
                                   tmp_path, bypass_guard=True)
    assert observed["outcome"] == "failed"
    assert payload.read_bytes() == b'{"old": true}'


def test_a_failed_endpoint_is_logged(tmp_path, fake_get, caplog):
    endpoint = core.ENDPOINTS[0]
    fake_get.set(endpoint.path, FakeResponse(b"<html>", 200))
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core._poll_endpoint(endpoint, LOOPBACK_BASE + endpoint.path,
                            tmp_path, bypass_guard=True)
    assert "FuelMix failed: invalid JSON" in caplog.text


def test_a_payload_that_cannot_be_written_is_an_internal_error(tmp_path,
                                                               good_get,
                                                               monkeypatch,
                                                               caplog):
    # "internal error" should mean only "a bug in our code", and a raw
    # directory that has gone read-only is close enough to one.
    def boom(path, payload):
        raise OSError("read-only file system")

    monkeypatch.setattr(core, "write_atomic", boom)
    endpoint = core.ENDPOINTS[0]
    with caplog.at_level(logging.ERROR, logger=core.log.name):
        observed = core._poll_endpoint(endpoint,
                                       LOOPBACK_BASE + endpoint.path,
                                       tmp_path, bypass_guard=True)
    assert observed["outcome"] == "failed"
    assert observed["last_error"] == "internal error"
    assert "could not write payload" in caplog.text


def test_a_successful_endpoint_is_logged_with_its_size(tmp_path, good_get,
                                                       caplog):
    endpoint = core.ENDPOINTS[0]
    with caplog.at_level(logging.INFO, logger=core.log.name):
        core._poll_endpoint(endpoint, LOOPBACK_BASE + endpoint.path,
                            tmp_path, bypass_guard=True)
    assert "FuelMix ok" in caplog.text


# --- _all_skipped_status ----------------------------------------------------

def test_an_all_skipped_cycle_with_no_prior_file_synthesizes_four_entries(
        raw):
    status = core._all_skipped_status(raw)
    assert set(status["endpoints"]) == set(ALL_KEYS)
    assert all(e["outcome"] == "skipped"
               for e in status["endpoints"].values())
    assert status["skipped"] is True


def test_an_all_skipped_cycle_returns_the_stored_status_unchanged(raw):
    write_status(raw, {"FuelMix": entry_for("FuelMix",
                                            consecutive_failures=4)})
    status = core._all_skipped_status(raw)
    assert status["endpoints"]["FuelMix"]["consecutive_failures"] == 4
    assert status["skipped"] is True


def test_an_all_skipped_cycle_does_not_mutate_the_stored_status(raw):
    write_status(raw, {"FuelMix": entry_for("FuelMix")})
    before = core.status_path(raw).read_bytes()
    core._all_skipped_status(raw)
    assert core.status_path(raw).read_bytes() == before


# --- poll_once, end to end --------------------------------------------------

def test_a_happy_cycle_writes_five_files(raw, good_get, monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    core.poll_once()
    written = sorted(p.name for p in raw.glob("*.json"))
    assert written == ["FuelMix.json", "RealTimeTotalLoad.json",
                       "Snapshot.json", "WindSolar.json", "_status.json"]


def test_a_happy_cycle_marks_every_endpoint_ok(raw, good_get, monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    status = core.poll_once()
    assert [e["outcome"] for e in status["endpoints"].values()] == \
        ["ok"] * 4


def test_a_happy_cycle_writes_each_payload_verbatim(raw, good_get,
                                                    monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    core.poll_once()
    for endpoint in core.ENDPOINTS:
        assert (raw / f"{endpoint.key}.json").read_bytes() == \
            GOOD_BODIES[endpoint.path]


def test_a_happy_cycle_records_each_ref_id(raw, good_get, monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    status = core.poll_once()
    found = {k: e["ref_id"] for k, e in status["endpoints"].items()}
    assert found == GOOD_REF_IDS


def test_a_happy_cycle_records_the_absolute_raw_dir(raw, good_get,
                                                    monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    status = core.poll_once()
    assert Path(status["raw_dir"]).is_absolute()
    assert Path(status["raw_dir"]) == raw


def test_a_happy_cycle_records_the_base_it_used(raw, good_get, monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    assert core.poll_once()["base_url"] == LOOPBACK_BASE


def test_a_happy_cycle_stamps_the_cycle_at_both_ends(raw, good_get,
                                                     monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    status = core.poll_once()
    started = datetime.fromisoformat(status["cycle_started_at"])
    finished = datetime.fromisoformat(status["cycle_finished_at"])
    assert finished >= started
    assert started.utcoffset() is not None


def test_a_happy_cycle_records_the_endpoint_path_on_every_entry(raw,
                                                                good_get,
                                                                monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    status = core.poll_once()
    for endpoint in core.ENDPOINTS:
        assert status["endpoints"][endpoint.key]["path"] == endpoint.path


def test_a_cycle_creates_the_raw_directory_if_it_is_missing(tmp_path,
                                                            good_get,
                                                            monkeypatch):
    target = tmp_path / "made" / "by" / "poll_once"
    monkeypatch.setenv("MISO_RAW_DIR", str(target))
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    core.poll_once()
    assert (target / "_status.json").exists()


def test_a_cycle_sweeps_a_stale_temp_file_before_fetching(raw, good_get,
                                                          monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    stale = raw / "FuelMix.old.tmp"
    stale.write_bytes(b"x")
    age(stale, core.STALE_TMP_SECONDS + 60)
    core.poll_once()
    assert not stale.exists()


def test_a_cycle_leaves_no_temp_files_behind(raw, good_get, monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    core.poll_once()
    assert list(raw.glob("*.tmp")) == []


@pytest.mark.parametrize("response,expected", [
    (FakeResponse(b"<html>503</html>", 200), "invalid JSON"),
    (FakeResponse(b"", 200), "invalid JSON"),
    (FakeResponse(b'{"nope": 1}', 200), "shape check failed"),
    (FakeResponse(b"body", 503), "HTTP 503"),
    (FakeResponse(b"body", 429), "HTTP 429"),
    (FakeResponse(b"", 302), "redirect 302"),
    (requests.exceptions.ConnectionError("refused"), "connection error"),
    (requests.exceptions.Timeout("slow"), "timeout"),
    (requests.exceptions.ChunkedEncodingError("cut"), "truncated response"),
    (requests.exceptions.MissingSchema("no scheme"), "bad base url"),
])
def test_each_failure_mode_lands_in_the_status_file(raw, good_get,
                                                    monkeypatch, response,
                                                    expected):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    good_get.set("/api/FuelMix", response)
    status = core.poll_once()
    entry = status["endpoints"]["FuelMix"]
    assert entry["outcome"] == "failed"
    assert entry["last_error"] == expected


@pytest.mark.parametrize("response", [
    FakeResponse(b"<html>503</html>", 200),
    FakeResponse(b"body", 503),
    FakeResponse(b"", 302),
    requests.exceptions.ConnectionError("refused"),
])
def test_a_failure_leaves_an_existing_payload_byte_identical(raw, good_get,
                                                             monkeypatch,
                                                             response):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    core.poll_once()
    before = (raw / "FuelMix.json").read_bytes()
    good_get.set("/api/FuelMix", response)
    core.poll_once()
    assert (raw / "FuelMix.json").read_bytes() == before


def test_one_failing_endpoint_does_not_stop_the_other_three(raw, good_get,
                                                            monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    good_get.set("/api/Snapshot", FakeResponse(b"body", 503))
    status = core.poll_once()
    outcomes = {k: e["outcome"] for k, e in status["endpoints"].items()}
    assert outcomes == {"FuelMix": "ok", "RealTimeTotalLoad": "ok",
                        "Snapshot": "failed", "WindSolar": "ok"}


def test_an_endpoint_raising_an_unexpected_exception_does_not_abort_the_cycle(
        raw, good_get, monkeypatch, caplog):
    # Anything arriving in poll_once's except clause is a bug in our code
    # and is reported as one, without taking the other three down.
    def explode(url):
        raise ValueError("a bug that is not a requests exception")

    good_get.set("/api/Snapshot", explode)
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    with caplog.at_level(logging.ERROR, logger=core.log.name):
        status = core.poll_once()
    assert status["endpoints"]["Snapshot"]["last_error"] == "internal error"
    assert core.succeeded_count(status) == 3
    assert "Snapshot: unexpected failure" in caplog.text


def test_consecutive_failures_count_up_across_cycles(raw, good_get,
                                                     monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    good_get.set("/api/FuelMix", FakeResponse(b"body", 503))
    for _ in range(3):
        status = core.poll_once()
    assert status["endpoints"]["FuelMix"]["consecutive_failures"] == 3


def test_a_success_resets_the_failure_count_and_clears_the_error(raw,
                                                                 good_get,
                                                                 monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    good_get.set("/api/FuelMix", FakeResponse(b"body", 503))
    core.poll_once()
    good_get.serve_good()
    status = core.poll_once()
    entry = status["endpoints"]["FuelMix"]
    assert entry["consecutive_failures"] == 0
    assert entry["last_error"] is None


def test_a_failed_cycle_carries_forward_last_success_and_ref_id(raw,
                                                                good_get,
                                                                monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    first = core.poll_once()["endpoints"]["FuelMix"]
    good_get.set("/api/FuelMix", FakeResponse(b"body", 503))
    second = core.poll_once()["endpoints"]["FuelMix"]
    assert second["last_success"] == first["last_success"]
    assert second["ref_id"] == first["ref_id"]
    assert second["ref_id_changed_at"] == first["ref_id_changed_at"]


def test_a_frozen_feed_stalls_ref_id_changed_at_while_last_success_advances(
        raw, good_get, monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    first = core.poll_once()["endpoints"]["FuelMix"]
    second = core.poll_once()["endpoints"]["FuelMix"]
    assert second["ref_id_changed_at"] == first["ref_id_changed_at"]
    assert second["last_success"] >= first["last_success"]


def test_a_new_ref_id_moves_ref_id_changed_at(raw, good_get, monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    first = core.poll_once()["endpoints"]["FuelMix"]
    good_get.set("/api/FuelMix", FakeResponse(
        b'{"RefId": "ref-fuelmix-2", "TotalMW": "1", "Fuel": {}}', 200))
    second = core.poll_once()["endpoints"]["FuelMix"]
    assert second["ref_id"] == "ref-fuelmix-2"
    assert second["ref_id_changed_at"] != first["ref_id_changed_at"]


def test_a_hand_edited_failure_count_is_reinitialized_not_raised_on(raw,
                                                                    good_get,
                                                                    monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    write_status(raw, {"FuelMix": entry_for("FuelMix",
                                            consecutive_failures="3")})
    good_get.set("/api/FuelMix", FakeResponse(b"body", 503))
    status = core.poll_once()
    assert status["endpoints"]["FuelMix"]["consecutive_failures"] == 1


def test_an_unreadable_status_file_does_not_stop_a_cycle(raw, good_get,
                                                         monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    core.status_path(raw).write_text("{{{")
    status = core.poll_once()
    assert core.succeeded_count(status) == 4


def test_a_deeply_nested_status_file_does_not_stop_a_cycle(raw, good_get,
                                                           monkeypatch):
    # RecursionError is not ValueError. This was a real bug.
    monkeypatch.setenv("MISO_API_BASE", LOOPBACK_BASE)
    core.status_path(raw).write_text("[" * 200_000 + "]" * 200_000)
    status = core.poll_once()
    assert core.succeeded_count(status) == 4


# --- poll_once when the rate guard skips everything -------------------------

def test_an_all_skipped_cycle_writes_no_status_file(raw, fake_get,
                                                    monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", GUARDED_BASE)
    monkeypatch.setattr(core.guard, "claim", lambda url: False)
    status = core.poll_once()
    assert status["skipped"] is True
    assert not core.status_path(raw).exists()


def test_an_all_skipped_cycle_reports_every_endpoint_skipped(raw, fake_get,
                                                             monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", GUARDED_BASE)
    monkeypatch.setattr(core.guard, "claim", lambda url: False)
    status = core.poll_once()
    assert all(e["outcome"] == "skipped"
               for e in status["endpoints"].values())


def test_an_all_skipped_cycle_leaves_a_prior_status_file_untouched(raw,
                                                                   fake_get,
                                                                   monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", GUARDED_BASE)
    write_status(raw, {"FuelMix": entry_for("FuelMix", ref_id="r-1",
                                            consecutive_failures=2)})
    before = core.status_path(raw).read_bytes()
    monkeypatch.setattr(core.guard, "claim", lambda url: False)
    status = core.poll_once()
    assert core.status_path(raw).read_bytes() == before
    assert status["endpoints"]["FuelMix"]["ref_id"] == "r-1"
    assert status["skipped"] is True


def test_an_all_skipped_cycle_leaves_payload_files_unpruned(raw, fake_get,
                                                            monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", GUARDED_BASE)
    (raw / "Departed.json").write_bytes(b"{}")
    write_status(raw, {"Departed": entry_for("FuelMix")})
    monkeypatch.setattr(core.guard, "claim", lambda url: False)
    core.poll_once()
    assert (raw / "Departed.json").exists()


def test_an_all_skipped_cycle_is_announced(raw, fake_get, monkeypatch,
                                           caplog):
    monkeypatch.setenv("MISO_API_BASE", GUARDED_BASE)
    monkeypatch.setattr(core.guard, "claim", lambda url: False)
    with caplog.at_level(logging.WARNING, logger=core.log.name):
        core.poll_once()
    assert "every endpoint was skipped" in caplog.text


def test_a_partly_skipped_cycle_still_writes_the_status_file(raw, good_get,
                                                             monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", GUARDED_BASE)
    monkeypatch.setattr(core.guard, "claim",
                        lambda url: not url.endswith("/api/FuelMix"))
    status = core.poll_once()
    assert "skipped" not in status
    assert status["endpoints"]["FuelMix"]["outcome"] == "skipped"
    assert status["endpoints"]["Snapshot"]["outcome"] == "ok"


def test_a_skip_does_not_increment_failures_or_overwrite_the_last_error(
        raw, good_get, monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", GUARDED_BASE)
    monkeypatch.setattr(core.guard, "claim", lambda url: True)
    good_get.set("/api/FuelMix", FakeResponse(b"body", 503))
    core.poll_once()

    monkeypatch.setattr(core.guard, "claim",
                        lambda url: not url.endswith("/api/FuelMix"))
    entry = core.poll_once()["endpoints"]["FuelMix"]
    assert entry["outcome"] == "skipped"
    assert entry["consecutive_failures"] == 1
    assert entry["last_error"] == "HTTP 503"


# --- succeeded_count --------------------------------------------------------

def test_succeeded_count_counts_only_this_cycles_successes():
    status = {"endpoints": {
        "a": {"outcome": "ok"},
        "b": {"outcome": "failed", "last_success": "1970-01-01T00:00:00"},
        "c": {"outcome": "skipped", "consecutive_failures": 0},
        "d": {"outcome": "ok"},
    }}
    assert core.succeeded_count(status) == 2


def test_succeeded_count_is_zero_for_a_status_with_no_endpoints():
    assert core.succeeded_count({}) == 0


def test_succeeded_count_ignores_an_entry_that_is_not_an_object():
    status = {"endpoints": {"a": "ok", "b": None, "c": {"outcome": "ok"}}}
    assert core.succeeded_count(status) == 1


def test_succeeded_count_ignores_an_entry_with_no_outcome():
    status = {"endpoints": {"a": {"last_success": "1970-01-01T00:00:00"}}}
    assert core.succeeded_count(status) == 0


# --- against the local stub -------------------------------------------------

def test_a_cycle_against_the_local_stub_succeeds(raw, stub_base,
                                                 monkeypatch):
    # Real HTTP, real gzip handling, 127.0.0.1 only.
    monkeypatch.setenv("MISO_API_BASE", stub_base())
    status = core.poll_once()
    assert core.succeeded_count(status) == 4
    assert (raw / "FuelMix.json").read_bytes().startswith(b"{")


def test_the_stubs_redirect_is_recorded_and_not_followed(raw, stub_base,
                                                         monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", stub_base(modes=["redirect"]))
    status = core.poll_once()
    errors = {e["last_error"] for e in status["endpoints"].values()}
    assert errors == {"redirect 302"}


def test_the_stubs_html_mode_is_recorded_as_invalid_json(raw, stub_base,
                                                         monkeypatch):
    monkeypatch.setenv("MISO_API_BASE", stub_base(modes=["html"]))
    status = core.poll_once()
    assert status["endpoints"]["FuelMix"]["last_error"] == "invalid JSON"


def test_the_stubs_empty_payloads_are_recorded_per_endpoint(raw, stub_base,
                                                            monkeypatch):
    base = stub_base(modes=["empty-load", "empty-snapshot"])
    monkeypatch.setenv("MISO_API_BASE", base)
    endpoints = core.poll_once()["endpoints"]
    assert endpoints["RealTimeTotalLoad"]["last_error"] == "missing ref_id"
    assert endpoints["Snapshot"]["last_error"] == "shape check failed"
    assert endpoints["FuelMix"]["outcome"] == "ok"


def test_a_stub_cycle_never_touches_the_real_rate_guard(raw, stub_base,
                                                        monkeypatch):
    # Loopback bypasses the guard, so no lease file is created at all.
    from backend.poller import guard

    monkeypatch.setenv("MISO_API_BASE", stub_base())
    core.poll_once()
    assert not guard.guard_path().exists()
