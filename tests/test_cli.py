"""Tests for backend/poller/__main__.py - the command line.

Nothing here makes a network request. `core.poll_once` is monkeypatched to
return a crafted status dict, which is the whole point: the exit-code
contract in spec 8.4 is a function of that dict and of nothing else, so it
can be pinned exactly without a socket. The --status tests go further and
assert no request was made, since 8.3 promises that flag touches no network.

conftest.py points XDG_CACHE_HOME and MISO_RAW_DIR at tmp_path for every
test in this file, so the real rate-guard lease file and the repo's data/
are never read or written.
"""

import json
import logging
import runpy
import sys
import warnings
from datetime import timedelta

import pytest

from backend.poller import __main__ as cli
from backend.poller import core


# --- helpers ----------------------------------------------------------------

def make_status(outcomes, **extra):
    """A status dict whose four endpoints carry the given outcomes.

    Built from core.INITIAL_ENTRY so it has the same shape a real cycle
    writes: consecutive_failures 0, last_success None, and so on.
    """
    endpoints = {}
    for endpoint, outcome in zip(core.ENDPOINTS, outcomes):
        entry = dict(core.INITIAL_ENTRY)
        entry["path"] = endpoint.path
        entry["outcome"] = outcome
        endpoints[endpoint.key] = entry
    status = {
        "base_url": core.DEFAULT_BASE_URL,
        "raw_dir": "unused",
        "cycle_started_at": core.now().isoformat(),
        "cycle_finished_at": core.now().isoformat(),
        "endpoints": endpoints,
    }
    status.update(extra)
    return status


def stub_poll_once(monkeypatch, status=None, error=None):
    """Replace core.poll_once with something that never touches the network."""
    calls = []

    def fake():
        calls.append(1)
        if error is not None:
            raise error
        return status

    monkeypatch.setattr(core, "poll_once", fake)
    return calls


def write_status_file(raw, text):
    """Put raw text at raw/_status.json, as a hand edit would."""
    path = core.status_path(raw)
    path.write_text(text)
    return path


# --- exit codes, spec 8.4 ---------------------------------------------------

def test_one_endpoint_ok_exits_0(monkeypatch):
    status = make_status(["ok", "failed", "failed", "failed"])
    stub_poll_once(monkeypatch, status)
    assert cli.run_once() == 0


def test_partial_cycle_some_ok_some_failed_exits_0(monkeypatch):
    status = make_status(["ok", "ok", "failed", "failed"])
    stub_poll_once(monkeypatch, status)
    assert cli.run_once() == 0


def test_every_endpoint_attempted_and_none_succeeded_exits_1(monkeypatch):
    status = make_status(["failed"] * 4)
    stub_poll_once(monkeypatch, status)
    assert cli.run_once() == 1


def test_every_endpoint_skipped_by_the_guard_exits_2(monkeypatch):
    status = make_status(["skipped"] * 4, skipped=True)
    stub_poll_once(monkeypatch, status)
    assert cli.run_once() == 2


def test_poll_once_raising_oserror_exits_1(monkeypatch):
    stub_poll_once(monkeypatch, error=OSError("data/raw is read-only"))
    assert cli.run_once() == 1


def test_poll_once_raising_oserror_logs_the_reason(monkeypatch, caplog):
    stub_poll_once(monkeypatch, error=OSError("data/raw is read-only"))
    with caplog.at_level(logging.ERROR, logger="backend.poller"):
        cli.run_once()
    assert "data/raw is read-only" in caplog.text


def test_mixed_skipped_and_failed_with_no_ok_exits_1(monkeypatch):
    # Spec 8.4's exit-1 row covers this: some endpoints guard-skipped, the
    # rest attempted and failed. Nothing usable was produced, and exit 2
    # would falsely claim nothing was tried.
    status = make_status(["skipped", "skipped", "failed", "failed"])
    stub_poll_once(monkeypatch, status)
    assert cli.run_once() == 1


def test_all_endpoints_failed_logs_the_count(monkeypatch, caplog):
    status = make_status(["failed"] * 4)
    stub_poll_once(monkeypatch, status)
    with caplog.at_level(logging.ERROR, logger="backend.poller"):
        cli.run_once()
    assert "all 4 endpoints failed" in caplog.text


def test_successful_cycle_logs_how_many_of_how_many(monkeypatch, caplog):
    status = make_status(["ok", "ok", "failed", "failed"])
    stub_poll_once(monkeypatch, status)
    with caplog.at_level(logging.INFO, logger="backend.poller"):
        cli.run_once()
    assert "2 of 4 endpoints succeeded" in caplog.text


# --- the ordering of the skipped check, spec 8.4 ----------------------------

def test_first_ever_run_skipped_everywhere_exits_2_not_0():
    # The bug this exists for: on a first-ever run with no stored status,
    # core synthesizes one INITIAL_ENTRY per endpoint, each carrying
    # consecutive_failures == 0. A success count computed before the
    # skipped check reads those four zeroes as four successes and exits 0,
    # reporting a healthy poll of a cycle that fetched nothing at all.
    status = {
        "endpoints": {
            endpoint.key: dict(core.INITIAL_ENTRY, path=endpoint.path,
                               outcome="skipped")
            for endpoint in core.ENDPOINTS
        },
        "skipped": True,
    }
    assert all(e["consecutive_failures"] == 0
               for e in status["endpoints"].values())
    assert core.succeeded_count(status) == 0


def test_skipped_flag_is_checked_before_the_success_count(monkeypatch):
    # Ordering, asserted directly: this dict would exit 0 if the success
    # count ran first, because every outcome is "ok". The skipped flag has
    # to win, so the answer is 2.
    status = make_status(["ok"] * 4, skipped=True)
    stub_poll_once(monkeypatch, status)
    assert cli.run_once() == 2


def test_all_skipped_logs_the_rate_guard_as_the_reason(monkeypatch, caplog):
    status = make_status(["skipped"] * 4, skipped=True)
    stub_poll_once(monkeypatch, status)
    with caplog.at_level(logging.WARNING, logger="backend.poller"):
        cli.run_once()
    assert "rate guard" in caplog.text


# --- argument parsing, spec 8.3 ---------------------------------------------

def test_no_flag_parses_as_all_three_modes_off():
    args = cli.parse_args([])
    assert (args.once, args.loop, args.status) == (False, False, False)


def test_no_flag_runs_one_cycle_because_once_is_the_default(monkeypatch):
    calls = stub_poll_once(monkeypatch, make_status(["ok"] * 4))
    assert cli.main([]) == 0
    assert len(calls) == 1


def test_explicit_once_runs_one_cycle(monkeypatch):
    calls = stub_poll_once(monkeypatch, make_status(["ok"] * 4))
    assert cli.main(["--once"]) == 0
    assert len(calls) == 1


def test_once_with_loop_is_an_argparse_error():
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["--once", "--loop"])
    assert exc.value.code == 2


def test_status_with_once_is_an_argparse_error():
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["--status", "--once"])
    assert exc.value.code == 2


def test_help_exits_0_and_lists_all_four_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--help", "--once", "--loop", "--status"):
        assert flag in out


def test_unknown_flag_exits_2():
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["--nonsense"])
    assert exc.value.code == 2


def test_module_entry_point_exits_via_systemexit(monkeypatch):
    # `python -m backend.poller --status` on an empty raw dir: the module's
    # __main__ guard has to turn main()'s return value into an exit code.
    monkeypatch.setattr(sys, "argv", ["python -m backend.poller", "--status"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("backend.poller", run_name="__main__")
    assert exc.value.code == 0


# --- helper functions -------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"),
    (45, "45s"),
    (59, "59s"),
    (60, "1m"),
    (180, "3m"),
    (3599, "59m"),
    (3600, "1h 0m"),
    (7500, "2h 5m"),
    (86400, "1d 0h"),
    (273600, "3d 4h"),
])
def test_duration_text_formats_each_magnitude(seconds, expected):
    assert cli.duration_text(seconds) == expected


def test_duration_text_truncates_fractional_seconds():
    assert cli.duration_text(45.9) == "45s"


def test_parse_stamp_returns_an_aware_datetime():
    moment = cli.parse_stamp("2026-09-04T10:00:00-04:00")
    assert moment is not None and moment.tzinfo is not None


def test_parse_stamp_rejects_a_naive_timestamp():
    # Not fussiness: age_text subtracts this from an aware reference, and
    # mixing aware and naive raises TypeError. The status file is hand
    # editable, so a naive stamp is a thing that arrives.
    assert cli.parse_stamp("2026-09-04T10:00:00") is None


def test_parse_stamp_rejects_a_non_string():
    assert cli.parse_stamp(1725465600) is None


def test_parse_stamp_rejects_an_empty_string():
    assert cli.parse_stamp("") is None


def test_parse_stamp_rejects_unparseable_text():
    assert cli.parse_stamp("yesterday") is None


def test_age_text_says_never_for_a_missing_timestamp():
    assert cli.age_text(None, core.now()) == "never"


def test_age_text_says_unreadable_for_a_naive_timestamp():
    assert cli.age_text("2026-09-04T10:00:00", core.now()) == "unreadable"


def test_age_text_says_unreadable_for_a_non_string():
    assert cli.age_text(17, core.now()) == "unreadable"


def test_age_text_reports_a_past_timestamp_as_an_age():
    now = core.now()
    stamp = (now - timedelta(minutes=3)).isoformat()
    assert cli.age_text(stamp, now) == "3m ago"


def test_age_text_reports_a_future_timestamp_plainly():
    now = core.now()
    stamp = (now + timedelta(hours=1)).isoformat()
    assert cli.age_text(stamp, now) == "in the future"


def endpoint_named(key):
    """The Endpoint row with this key, so tests do not index the table."""
    return next(e for e in core.ENDPOINTS if e.key == key)


def test_feed_text_says_na_for_an_endpoint_with_no_ref_id():
    # Snapshot has no RefId at any level, so it has no frozen-feed signal.
    # Printing "unknown" there would imply a fault that does not exist.
    text = cli.feed_text({}, endpoint_named("Snapshot"), core.now())
    assert text == "n/a - this endpoint has no ref_id"


def test_feed_text_says_unknown_when_there_is_no_successful_fetch_yet():
    text = cli.feed_text({}, endpoint_named("FuelMix"), core.now())
    assert text == "unknown - no successful fetch yet"


def test_feed_text_reports_a_stalled_ref_id_as_frozen():
    now = core.now()
    entry = {
        "ref_id_changed_at": (now - timedelta(hours=2)).isoformat(),
        "last_success": now.isoformat(),
    }
    text = cli.feed_text(entry, endpoint_named("FuelMix"), now)
    assert text.startswith("FROZEN - ref_id changed 2h 0m ago")


def test_feed_text_reports_a_current_ref_id_as_moving():
    now = core.now()
    entry = {
        "ref_id_changed_at": (now - timedelta(minutes=2)).isoformat(),
        "last_success": now.isoformat(),
    }
    text = cli.feed_text(entry, endpoint_named("FuelMix"), now)
    assert text.startswith("moving - ref_id changed 2m ago")


def test_feed_text_is_unknown_when_only_the_ref_id_stamp_is_readable():
    now = core.now()
    entry = {"ref_id_changed_at": now.isoformat(), "last_success": None}
    text = cli.feed_text(entry, endpoint_named("FuelMix"), now)
    assert text == "unknown - no successful fetch yet"


# --- --status ---------------------------------------------------------------

def test_status_with_no_file_says_no_cycle_has_finished(raw, capsys):
    assert cli.run_status() == 0
    out = capsys.readouterr().out
    assert "no cycle has finished" in out
    assert cli.RUN_HINT in out


def test_status_with_no_file_still_prints_the_raw_dir(raw, capsys):
    cli.run_status()
    assert str(raw) in capsys.readouterr().out


def populated_status(raw, **overrides):
    """A realistic, readable _status.json written to raw."""
    now = core.now()
    endpoints = {}
    changed = (now - timedelta(minutes=2)).isoformat()
    for endpoint in core.ENDPOINTS:
        has_ref = endpoint.ref_path is not None
        endpoints[endpoint.key] = {
            "path": endpoint.path,
            "outcome": "ok",
            "last_attempt": now.isoformat(),
            "last_success": now.isoformat(),
            "consecutive_failures": 0,
            "last_error": None,
            "http_status": 200,
            "bytes": 1024,
            "ref_id": "26-Aug-2026" if has_ref else None,
            "ref_id_changed_at": changed if has_ref else None,
        }
    status = {
        "base_url": core.DEFAULT_BASE_URL,
        "raw_dir": str(raw),
        "cycle_started_at": now.isoformat(),
        "cycle_finished_at": now.isoformat(),
        "endpoints": endpoints,
    }
    status.update(overrides)
    write_status_file(raw, json.dumps(status))
    return status


def test_status_with_a_populated_file_prints_all_four_endpoints(raw,
                                                                capsys):
    populated_status(raw)
    assert cli.run_status() == 0
    out = capsys.readouterr().out
    for endpoint in core.ENDPOINTS:
        assert endpoint.key in out


def test_status_prints_the_no_network_reassurance(raw, capsys):
    populated_status(raw)
    cli.run_status()
    assert "No network request was made." in capsys.readouterr().out


def test_status_makes_no_network_request(raw, capsys, fake_get):
    # Spec 8.3: --status is derived entirely from _status.json. fake_get
    # replaces requests.get with a routing table that has no routes, so any
    # request at all raises AssertionError instead of reaching a socket.
    populated_status(raw)
    assert cli.run_status() == 0
    assert "MISO poller status" in capsys.readouterr().out


def test_status_prints_a_stored_last_error(raw, capsys):
    status = populated_status(raw)
    status["endpoints"]["FuelMix"]["last_error"] = "HTTP 503"
    status["endpoints"]["FuelMix"]["outcome"] = "failed"
    status["endpoints"]["FuelMix"]["consecutive_failures"] = 5
    write_status_file(raw, json.dumps(status))
    cli.run_status()
    assert "last error: HTTP 503" in capsys.readouterr().out


def test_status_flags_a_base_url_that_is_not_the_default(raw, capsys):
    populated_status(raw, base_url="http://127.0.0.1:8765")
    cli.run_status()
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8765" in out
    assert "not the default base" in out


def test_status_does_not_flag_the_default_base_url(raw, capsys):
    populated_status(raw)
    cli.run_status()
    assert "not the default base" not in capsys.readouterr().out


def test_status_reports_a_raw_dir_that_disagrees_with_the_one_read(raw,
                                                                   capsys):
    populated_status(raw, raw_dir="/somewhere/else")
    cli.run_status()
    assert "the two disagree" in capsys.readouterr().out


def test_status_reports_a_frozen_feed(raw, capsys):
    now = core.now()
    status = populated_status(raw)
    entry = status["endpoints"]["FuelMix"]
    entry["last_success"] = now.isoformat()
    entry["ref_id_changed_at"] = (now - timedelta(hours=3)).isoformat()
    write_status_file(raw, json.dumps(status))
    cli.run_status()
    assert "FROZEN" in capsys.readouterr().out


def test_status_reports_a_moving_feed(raw, capsys):
    populated_status(raw)
    cli.run_status()
    out = capsys.readouterr().out
    assert "moving - ref_id changed" in out
    assert "FROZEN" not in out


def test_status_prints_the_na_form_for_snapshot(raw, capsys):
    # Snapshot never has a ref_id, so it must not be reported as unknown or
    # frozen - both would imply a fault.
    populated_status(raw)
    cli.run_status()
    assert "n/a - this endpoint has no ref_id" in capsys.readouterr().out


def test_status_on_an_entry_that_is_missing_outcome(raw, capsys):
    # The upgrade path: a file written by code that predates `outcome`.
    # --status reads carried-forward fields only, so it must still print.
    status = populated_status(raw)
    for entry in status["endpoints"].values():
        entry.pop("outcome")
    write_status_file(raw, json.dumps(status))
    assert cli.run_status() == 0
    assert "FuelMix" in capsys.readouterr().out


def test_status_on_a_file_missing_one_endpoint_entry(raw, capsys):
    status = populated_status(raw)
    del status["endpoints"]["WindSolar"]
    write_status_file(raw, json.dumps(status))
    assert cli.run_status() == 0
    out = capsys.readouterr().out
    assert "WindSolar" in out
    assert "no entry" in out


MALFORMED_STATUS_FILES = [
    ("unparseable", "{{{"),
    ("json null", "null"),
    ("json list", "[]"),
    ("empty object", "{}"),
    ("endpoints as a list", '{"endpoints": []}'),
    ("endpoints as a string", '{"endpoints": "four"}'),
    ("an entry that is an int", '{"endpoints": {"FuelMix": 7}}'),
    ("an entry that is null", '{"endpoints": {"FuelMix": null}}'),
    ("consecutive_failures as true",
     '{"endpoints": {"FuelMix": {"consecutive_failures": true}}}'),
    ("a naive timestamp",
     '{"endpoints": {"FuelMix": {"last_success": "2026-09-04T10:00:00"}}}'),
    ("a future timestamp",
     '{"endpoints": {"FuelMix":'
     ' {"last_success": "2999-01-01T00:00:00+00:00"}}}'),
    ("raw_dir as a dict", '{"raw_dir": {"path": "x"}, "endpoints": {}}'),
    ("base_url as a number", '{"base_url": 8, "endpoints": {}}'),
    ("an entry with no fields at all", '{"endpoints": {"FuelMix": {}}}'),
    ("a file that is empty", ""),
    ("a file of whitespace", "   \n  "),
]


@pytest.mark.parametrize("label,text", MALFORMED_STATUS_FILES,
                         ids=[label for label, _ in MALFORMED_STATUS_FILES])
def test_status_never_raises_on_a_malformed_file(raw, capsys, label, text):
    # --status is the command an operator runs to find out what is wrong.
    # It has to survive the very file it was run to explain.
    write_status_file(raw, text)
    assert cli.run_status() == 0
    out = capsys.readouterr().out
    assert out.startswith("MISO poller status")
    assert str(raw) in out


def test_status_survives_a_deeply_nested_file(raw, capsys):
    # json raises RecursionError, not ValueError, on a file nested this
    # deep. Letting it escape killed the one diagnostic meant to explain
    # the corrupt file.
    depth = 200_000
    write_status_file(raw, "[" * depth + "]" * depth)
    assert cli.run_status() == 0
    assert "not a usable status file" in capsys.readouterr().out


def test_status_from_main_returns_0(raw, capsys):
    populated_status(raw)
    assert cli.main(["--status"]) == 0
    assert "MISO poller status" in capsys.readouterr().out


# --- --loop -----------------------------------------------------------------

def sleep_that_stops_after(monkeypatch, iterations):
    """Replace time.sleep so the loop runs `iterations` times then Ctrl-Cs."""
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= iterations:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    return sleeps


def test_loop_runs_several_cycles_and_exits_0_on_ctrl_c(monkeypatch):
    calls = stub_poll_once(monkeypatch, make_status(["ok"] * 4))
    sleeps = sleep_that_stops_after(monkeypatch, 3)
    assert cli.main(["--loop"]) == 0
    assert len(calls) == 3
    assert sleeps == [core.DEFAULT_POLL_SECONDS] * 3


def test_loop_sleeps_for_the_configured_interval(monkeypatch):
    monkeypatch.setenv("MISO_POLL_SECONDS", "12")
    stub_poll_once(monkeypatch, make_status(["ok"] * 4))
    sleeps = sleep_that_stops_after(monkeypatch, 1)
    assert cli.main(["--loop"]) == 0
    assert sleeps == [12]


def test_loop_logs_that_it_stopped_rather_than_raising(monkeypatch, caplog):
    stub_poll_once(monkeypatch, make_status(["ok"] * 4))
    sleep_that_stops_after(monkeypatch, 1)
    with caplog.at_level(logging.INFO, logger="backend.poller"):
        assert cli.main(["--loop"]) == 0
    assert "stopped" in caplog.text


def test_a_cycle_that_raises_does_not_stop_the_loop(monkeypatch, caplog):
    # run_once only catches OSError, so anything else reaches run_loop.
    # One bad cycle must not end a long-running poller.
    calls = stub_poll_once(monkeypatch, error=RuntimeError("a bug"))
    sleeps = sleep_that_stops_after(monkeypatch, 3)
    with caplog.at_level(logging.ERROR, logger="backend.poller"):
        assert cli.main(["--loop"]) == 0
    assert len(calls) == 3
    assert len(sleeps) == 3
    assert "cycle raised, continuing" in caplog.text


def test_a_failing_cycle_does_not_stop_the_loop(monkeypatch):
    # An OSError is handled inside run_once and returns exit 1. The loop
    # drops that code on purpose and keeps polling.
    calls = stub_poll_once(monkeypatch, error=OSError("disk full"))
    sleep_that_stops_after(monkeypatch, 2)
    assert cli.main(["--loop"]) == 0
    assert len(calls) == 2
