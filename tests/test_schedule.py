"""Tests for backend/poller/schedule.py - the APScheduler wiring.

These call poller_blocked(), run_cycle(), start_scheduler() and
stop_scheduler() directly rather than booting FastAPI. The unit under test
is the scheduling, and dragging a web framework in front of it would only
make the same assertions slower and less specific.

Nothing here reaches MISO: core.poll_once is stubbed for every test in this
module by the autouse fixture below, which matters more here than anywhere
else because a real scheduler fires its first cycle immediately
(next_run_time=now, spec 8.5). conftest.py points XDG_CACHE_HOME and
MISO_RAW_DIR at tmp_path, so the real lease file and the repo's data/ are
untouched either way.

Out of scope, deliberately: Ctrl-C responsiveness and the daemon-thread
behavior of DaemonThreadExecutor. Proving the interpreter does not join a
stuck cycle needs a subprocess and a real signal, which pytest cannot do
honestly in-process. That has been measured manually.
"""

import logging
import sys
import threading
import types

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from backend.poller import core
from backend.poller import schedule

# Captured before no_real_resync (autouse) replaces it, so the two tests that
# exercise resync_rag itself can still reach the real one.
REAL_RESYNC = schedule.resync_rag


@pytest.fixture(autouse=True)
def no_real_cycles(monkeypatch):
    """Stub core.poll_once for every test here. Yields the call log."""
    calls = []

    def fake_poll_once():
        calls.append(1)
        return {"endpoints": {}}

    monkeypatch.setattr(core, "poll_once", fake_poll_once)
    return calls


@pytest.fixture(autouse=True)
def no_real_resync(monkeypatch):
    """Stub schedule.resync_rag for every test here. Yields the call log.

    Autouse for the same reason no_real_cycles is. run_cycle re-syncs the RAG
    store after every poll, and the real one imports chromadb and writes the
    repo's own data/chroma - which conftest cannot redirect, because the
    Chroma path is fixed in backend/rag/store.py rather than read from the
    environment. start_scheduler fires a cycle immediately, so this catches
    the scheduler tests too, not just the run_cycle ones.
    """
    calls = []
    monkeypatch.setattr(schedule, "resync_rag", lambda: calls.append(1))
    return calls


@pytest.fixture(autouse=True)
def no_leaked_cycles():
    """Fail any test here that leaves a poll cycle running behind it.

    stop_scheduler calls shutdown(wait=False) by design - DaemonThreadExecutor
    exists so a stuck cycle cannot hold up Ctrl-C - so shutdown returns while a
    cycle may still be in flight. A cycle that outlives its test outlives
    monkeypatch with it, and then resolves the real core.poll_once, against the
    real https://public-api.misoenergy.org, writing the repo's own data/raw.
    That was measured, not theorized.

    Autouse and asserting rather than merely joining, for the same reason
    conftest's cache_dir is autouse: an isolation rule that depends on every
    future test author remembering it is not a rule. conftest.py also refuses
    non-loopback requests at the socket, so the request cannot land even if
    this fixture is somehow bypassed.
    """
    yield
    for thread in threading.enumerate():
        if thread.name == "miso-poll":
            thread.join(timeout=10)
            assert not thread.is_alive(), (
                "a poll cycle outlived its test - it can now reach the real "
                "MISO, because monkeypatch has already been unwound")


@pytest.fixture
def stopper():
    """Guarantees every scheduler a test starts is shut down again."""
    started = []
    yield started
    for scheduler in started:
        schedule.stop_scheduler(scheduler)


class FakeScheduler:
    """A stand-in for BackgroundScheduler that can fail on demand."""

    def __init__(self, *args, fail_on=None, **kwargs):
        self.fail_on = fail_on
        self.jobs = []
        self.started = False
        self.shutdown_calls = []

    def add_job(self, func, trigger, **kwargs):
        if self.fail_on == "add_job":
            raise ValueError("job store refused the job")
        self.jobs.append((func, trigger, kwargs))

    def start(self):
        if self.fail_on == "start":
            raise RuntimeError("could not start the scheduler thread")
        self.started = True

    def shutdown(self, wait=True):
        self.shutdown_calls.append(wait)


def install_fake_scheduler(monkeypatch, fail_on=None):
    """Make start_scheduler build FakeSchedulers. Returns the built list."""
    built = []

    def factory(*args, **kwargs):
        fake = FakeScheduler(*args, fail_on=fail_on, **kwargs)
        built.append(fake)
        return fake

    monkeypatch.setattr(schedule, "BackgroundScheduler", factory)
    return built


# --- poller_blocked ---------------------------------------------------------

def test_poller_blocked_returns_none_when_nothing_is_wrong(raw):
    assert schedule.poller_blocked() is None


def test_poller_blocked_creates_the_raw_directory(tmp_path, monkeypatch):
    target = tmp_path / "not" / "yet" / "there"
    monkeypatch.setenv("MISO_RAW_DIR", str(target))
    assert schedule.poller_blocked() is None
    assert target.is_dir()


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", ""])
def test_poller_blocked_reports_every_spelling_of_off(monkeypatch, value):
    monkeypatch.setenv("MISO_POLLER_ENABLED", value)
    blocked = schedule.poller_blocked()
    assert blocked is not None
    level, reason = blocked
    assert level == logging.WARNING
    assert "MISO_POLLER_ENABLED" in reason


@pytest.mark.parametrize("value", ["1", "true", "yes", "anything"])
def test_poller_blocked_allows_every_spelling_of_on(monkeypatch, raw,
                                                    value):
    monkeypatch.setenv("MISO_POLLER_ENABLED", value)
    assert schedule.poller_blocked() is None


def test_poller_blocked_reports_an_unusable_raw_directory(tmp_path,
                                                          monkeypatch):
    # A regular file where the directory should be. mkdir(exist_ok=True)
    # still raises FileExistsError for a non-directory.
    blocker = tmp_path / "raw-is-a-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("MISO_RAW_DIR", str(blocker))
    blocked = schedule.poller_blocked()
    assert blocked is not None
    level, reason = blocked
    assert level == logging.ERROR
    assert str(blocker) in reason


def test_an_unusable_raw_directory_names_the_directory_that_failed(
        tmp_path, monkeypatch):
    # The message has to name the directory actually used, not a second
    # core.raw() call that could disagree.
    blocker = tmp_path / "occupied"
    blocker.write_text("x")
    monkeypatch.setenv("MISO_RAW_DIR", str(blocker))
    _, reason = schedule.poller_blocked()
    assert reason.startswith(f"cannot create {blocker}")


# --- run_cycle --------------------------------------------------------------

def test_run_cycle_runs_one_cycle(no_real_cycles):
    schedule.run_cycle()
    assert len(no_real_cycles) == 1


def test_run_cycle_returns_none_rather_than_a_status(no_real_cycles):
    assert schedule.run_cycle() is None


def test_run_cycle_swallows_an_exception_from_the_cycle(monkeypatch):
    # A cycle that threw would take the whole schedule down with it.
    def boom():
        raise RuntimeError("MISO returned something absurd")

    monkeypatch.setattr(core, "poll_once", boom)
    assert schedule.run_cycle() is None


def test_run_cycle_logs_a_failed_cycle_at_error(monkeypatch, caplog):
    def boom():
        raise RuntimeError("MISO returned something absurd")

    monkeypatch.setattr(core, "poll_once", boom)
    with caplog.at_level(logging.ERROR, logger=schedule.__name__):
        schedule.run_cycle()
    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert records
    assert "staying on schedule" in records[0].message


def test_run_cycle_swallows_an_oserror_too(monkeypatch):
    def boom():
        raise OSError("data/raw is read-only")

    monkeypatch.setattr(core, "poll_once", boom)
    assert schedule.run_cycle() is None


def test_run_cycle_resyncs_the_rag_store_after_polling(no_real_resync):
    # The whole point of the change: the poller refreshed data/raw/ every 5
    # minutes while Chroma only ever synced at boot, so answers froze at
    # boot-time data until someone restarted the server.
    schedule.run_cycle()
    assert len(no_real_resync) == 1


def test_run_cycle_resyncs_even_when_the_poll_failed(monkeypatch,
                                                     no_real_resync):
    # data/raw/ still holds the last good files, and the sync is idempotent.
    def boom():
        raise RuntimeError("MISO is down")

    monkeypatch.setattr(core, "poll_once", boom)
    schedule.run_cycle()
    assert len(no_real_resync) == 1


def test_run_cycle_swallows_a_failing_resync(monkeypatch, no_real_cycles):
    def boom():
        raise RuntimeError("chroma is unhappy")

    monkeypatch.setattr(schedule, "resync_rag", boom)
    assert schedule.run_cycle() is None
    assert len(no_real_cycles) == 1


def test_a_failing_resync_is_not_reported_as_a_failed_poll(monkeypatch,
                                                           caplog):
    # Sharing one handler would blame the network for a database problem.
    def boom():
        raise RuntimeError("chroma is unhappy")

    monkeypatch.setattr(schedule, "resync_rag", boom)
    with caplog.at_level(logging.ERROR, logger=schedule.__name__):
        schedule.run_cycle()
    messages = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert any("re-sync failed" in m for m in messages)
    assert not any("poll cycle failed" in m for m in messages)


# --- resync_rag -------------------------------------------------------------
#
# resync_rag imports backend.rag.ingest_api lazily, so these hand it a stub
# module through sys.modules rather than importing the real RAG lane, which
# would pull in chromadb and torch and write the repo's own data/chroma.

def _stub_rag_lane(monkeypatch, results):
    """Make the lazy `from backend.rag.ingest_api import ...` resolve to a stub."""
    module = types.ModuleType("backend.rag.ingest_api")
    module.sync_raw_snapshots = lambda: results
    monkeypatch.setitem(sys.modules, "backend.rag.ingest_api", module)


def test_resync_rag_reports_a_complete_sync_at_info(monkeypatch, caplog):
    _stub_rag_lane(monkeypatch, {"FuelMix.json": True, "Snapshot.json": True})
    with caplog.at_level(logging.INFO, logger=schedule.__name__):
        REAL_RESYNC()
    rendered = [r.getMessage() for r in caplog.records]
    assert any("re-synced from 2 snapshot files" in m for m in rendered)


def test_resync_rag_names_the_files_that_did_not_sync(monkeypatch, caplog):
    # Silence here means nobody can tell "synced fine" from "never ran".
    _stub_rag_lane(monkeypatch, {"FuelMix.json": True, "Snapshot.json": False})
    with caplog.at_level(logging.WARNING, logger=schedule.__name__):
        REAL_RESYNC()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    rendered = warnings[0].getMessage()
    assert "1 of 2" in rendered
    assert "Snapshot.json" in rendered


# --- start_scheduler --------------------------------------------------------

def test_start_scheduler_registers_exactly_one_job(raw, stopper):
    # Spec 8.5: exactly one job, not a one-shot plus an interval - two jobs
    # fire two cycles at t=0.
    scheduler = schedule.start_scheduler()
    stopper.append(scheduler)
    assert scheduler is not None
    assert len(scheduler.get_jobs()) == 1


def test_the_registered_job_is_named_miso_poll(raw, stopper):
    scheduler = schedule.start_scheduler()
    stopper.append(scheduler)
    assert scheduler.get_jobs()[0].id == "miso_poll"


def test_the_job_interval_matches_miso_poll_seconds(raw, monkeypatch,
                                                    stopper):
    monkeypatch.setenv("MISO_POLL_SECONDS", "7")
    scheduler = schedule.start_scheduler()
    stopper.append(scheduler)
    job = scheduler.get_jobs()[0]
    assert job.trigger.interval.total_seconds() == 7


def test_the_job_uses_the_settings_the_spec_names(raw, stopper):
    # coalesce and max_instances match APScheduler's defaults and are
    # spelled out anyway; misfire_grace_time does not, and a laptop that
    # slept would otherwise drop the cycle that came due.
    scheduler = schedule.start_scheduler()
    stopper.append(scheduler)
    job = scheduler.get_jobs()[0]
    assert job.coalesce is True
    assert job.max_instances == 1
    assert job.misfire_grace_time == 300


def test_the_started_scheduler_is_running(raw, stopper):
    scheduler = schedule.start_scheduler()
    stopper.append(scheduler)
    assert scheduler.running


def test_start_scheduler_logs_the_interval_it_registered(raw, caplog,
                                                         monkeypatch, stopper):
    monkeypatch.setenv("MISO_POLL_SECONDS", "11")
    with caplog.at_level(logging.INFO, logger=schedule.__name__):
        stopper.append(schedule.start_scheduler())
    assert "poller scheduled every 11 seconds" in caplog.text


def test_the_scheduled_job_actually_runs_a_cycle(raw, monkeypatch,
                                                 stopper):
    # The first cycle is scheduled for now, so starting the scheduler is
    # enough to make it fire. This also exercises DaemonThreadExecutor.
    ran = threading.Event()
    monkeypatch.setattr(core, "poll_once", lambda: ran.set())
    scheduler = schedule.start_scheduler()
    stopper.append(scheduler)
    assert ran.wait(timeout=10)


def test_start_scheduler_returns_none_when_construction_raises(raw,
                                                               monkeypatch):
    def refuse(*args, **kwargs):
        raise TypeError("unsupported executor")

    monkeypatch.setattr(schedule, "BackgroundScheduler", refuse)
    assert schedule.start_scheduler() is None


def test_start_scheduler_returns_none_when_add_job_raises(raw,
                                                          monkeypatch):
    install_fake_scheduler(monkeypatch, fail_on="add_job")
    assert schedule.start_scheduler() is None


def test_start_scheduler_returns_none_when_start_raises(raw, monkeypatch):
    install_fake_scheduler(monkeypatch, fail_on="start")
    assert schedule.start_scheduler() is None


@pytest.mark.parametrize("fail_on", ["add_job", "start"])
def test_a_failed_start_is_logged_at_error(raw, monkeypatch, caplog,
                                           fail_on):
    install_fake_scheduler(monkeypatch, fail_on=fail_on)
    with caplog.at_level(logging.ERROR, logger=schedule.__name__):
        schedule.start_scheduler()
    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert records
    assert "no poll job registered" in records[0].message


@pytest.mark.parametrize("fail_on", ["add_job", "start"])
def test_a_half_built_scheduler_is_shut_down_on_the_way_out(raw,
                                                            monkeypatch,
                                                            fail_on):
    built = install_fake_scheduler(monkeypatch, fail_on=fail_on)
    schedule.start_scheduler()
    assert built[0].shutdown_calls == [False]


def test_a_scheduler_that_will_not_start_raises_nothing_at_the_caller(
        raw, monkeypatch):
    # The app-facing contract is "no scheduler", never an exception: the
    # poller is a background convenience and the API is the demo.
    install_fake_scheduler(monkeypatch, fail_on="start")
    result = schedule.start_scheduler()
    assert result is None


def test_an_unparseable_poll_interval_still_schedules_a_job(raw,
                                                            monkeypatch,
                                                            stopper):
    # core.poll_seconds warns and falls back to 300 rather than raising, so
    # a typo in the environment must not cost the app its poll job.
    monkeypatch.setenv("MISO_POLL_SECONDS", "five")
    scheduler = schedule.start_scheduler()
    stopper.append(scheduler)
    assert scheduler is not None
    job = scheduler.get_jobs()[0]
    assert job.trigger.interval.total_seconds() == core.DEFAULT_POLL_SECONDS


# --- stop_scheduler ---------------------------------------------------------

def test_stop_scheduler_is_safe_when_none_was_ever_created():
    assert schedule.stop_scheduler(None) is None


def test_stop_scheduler_is_safe_on_a_scheduler_that_never_started():
    # shutdown() raises outright on a scheduler start() never ran. A
    # scheduler that died on the way up must not raise a second time on the
    # way down, where the traceback would be about teardown instead.
    scheduler = BackgroundScheduler()
    assert schedule.stop_scheduler(scheduler) is None


def test_stop_scheduler_logs_a_never_started_scheduler_below_error(caplog):
    scheduler = BackgroundScheduler()
    with caplog.at_level(logging.DEBUG, logger=schedule.__name__):
        schedule.stop_scheduler(scheduler)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_stop_scheduler_stops_a_running_scheduler(raw):
    scheduler = schedule.start_scheduler()
    assert scheduler.running
    schedule.stop_scheduler(scheduler)
    assert not scheduler.running


def test_stop_scheduler_does_not_wait_for_a_running_cycle(raw,
                                                          monkeypatch):
    # wait=False is what lets Ctrl-C exit while a cycle is still in flight.
    install_fake_scheduler(monkeypatch)
    scheduler = schedule.start_scheduler()
    schedule.stop_scheduler(scheduler)
    assert scheduler.shutdown_calls == [False]


# --- DaemonThreadExecutor ---------------------------------------------------
#
# Only the reporting contract is asserted here. Whether a daemon thread is
# genuinely abandoned at interpreter exit - the reason this executor exists
# at all - needs a subprocess and a real signal, and is out of scope above.

class FakeJob:
    """The two attributes the executor reads off an APScheduler job."""

    id = "miso_poll"
    _jobstore_alias = "default"


def test_the_executor_hands_back_an_error_from_the_job_runner(monkeypatch):
    # A daemon thread that died with an unhandled exception would take the
    # job's error reporting with it, and the scheduler would never know the
    # cycle did not run.
    errors = []
    done = threading.Event()

    def explode(*args, **kwargs):
        raise RuntimeError("run_job itself broke")

    def record(job_id, exc, traceback):
        errors.append(exc)
        done.set()

    monkeypatch.setattr(schedule, "run_job", explode)
    executor = schedule.DaemonThreadExecutor()
    executor._logger = logging.getLogger("test.executor")
    monkeypatch.setattr(executor, "_run_job_error", record)

    executor._do_submit_job(FakeJob(), [core.now()])
    assert done.wait(timeout=5)
    assert isinstance(errors[0], RuntimeError)


def test_the_executor_runs_the_job_on_a_daemon_thread(monkeypatch):
    # Whether the interpreter actually abandons the thread at exit needs a
    # subprocess and a real signal, and stays out of scope per the module
    # docstring. The flag itself is the reason this executor exists at all,
    # and it is assertable here: APScheduler's default pool joins its
    # workers on the way out no matter what their daemon flag says, so a
    # cycle stuck on a slow MISO would hold the terminal for up to 80
    # seconds - exactly when someone is reaching for Ctrl-C.
    made = []
    real_thread = threading.Thread

    def record(**kwargs):
        made.append(kwargs)
        return real_thread(**kwargs)

    monkeypatch.setattr(schedule.threading, "Thread", record)
    monkeypatch.setattr(schedule, "run_job", lambda *a, **k: [])
    executor = schedule.DaemonThreadExecutor()
    executor._logger = logging.getLogger("test.executor")
    monkeypatch.setattr(executor, "_run_job_success", lambda *a: None)

    executor._do_submit_job(FakeJob(), [core.now()])
    assert made[0]["daemon"] is True
    assert made[0]["name"] == "miso-poll"


def test_the_executor_hands_back_a_successful_run(monkeypatch):
    events = []
    done = threading.Event()

    def record(job_id, run_events):
        events.append(run_events)
        done.set()

    monkeypatch.setattr(schedule, "run_job", lambda *a, **k: ["one event"])
    executor = schedule.DaemonThreadExecutor()
    executor._logger = logging.getLogger("test.executor")
    monkeypatch.setattr(executor, "_run_job_success", record)

    executor._do_submit_job(FakeJob(), [core.now()])
    assert done.wait(timeout=5)
    assert events[0] == ["one event"]
