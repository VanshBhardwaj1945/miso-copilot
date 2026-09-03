"""Fetch four MISO public JSON endpoints and write them verbatim to data/raw/.

This is the whole job of the ingestion lane. It does not interpret what it
downloads: the bytes MISO returns are the bytes written to disk. The RAG lane
reads those files and everything downstream - prose, embedding, Chroma,
retrieval - belongs to it.

Two deliberate exceptions to "does not interpret": a shape gate that rejects
HTML error pages and empty payloads, and a `ref_id` lifted out of each payload
so that a frozen feed is visible in the status file.

Every endpoint entry in `_status.json` carries an `outcome` of "ok", "failed"
or "skipped". It is the only field describing THIS cycle and nothing else:
`consecutive_failures`, `last_success` and `ref_id` are carried forward across
cycles on purpose, so none of them can tell a cycle where everything failed
from one where a rate-guard skip inherited a zero from an earlier success.
Exit codes are read from `outcome` for exactly that reason.

Entry point is poll_once(). See the specification, sections 5 and 6.
"""

import ipaddress
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from backend.poller import guard

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://public-api.misoenergy.org"
TIMEZONE = ZoneInfo("America/Indiana/Indianapolis")
TIMEOUT = (5, 15)
HEADERS = {
    "User-Agent": "miso-copilot/0.1 (MISO Xtern Challenge 2026)",
    "Accept": "application/json",
}
STALE_TMP_SECONDS = 600
DEFAULT_POLL_SECONDS = 300
MIN_POLL_SECONDS = 5
MAX_POLL_SECONDS = 3600


# --- the endpoint table -----------------------------------------------------
#
# Four endpoints, each with the shape we require and where its RefId lives.
# `ref_path` is None for Snapshot, which carries no RefId at any level, so
# Snapshot is exempt from the ref_id gate and has no frozen-feed signal.

def _shape_fuelmix(body) -> bool:
    return isinstance(body, dict) and {"RefId", "TotalMW", "Fuel"} <= set(body)


def _shape_load(body) -> bool:
    return isinstance(body, dict) and "LoadInfo" in body


def _shape_snapshot(body) -> bool:
    """Non-empty array whose every row carries the four fields MISO publishes.

    The row check is not decoration. Snapshot is the one endpoint with no
    RefId, so it gets no protection from the ref_id gate, and it carries
    Current Demand and Marginal Energy Cost - the numbers a judge asks for
    first. Without this, `[{}]` would overwrite good data and record success.
    """
    return (
        isinstance(body, list)
        and len(body) > 0
        and all(isinstance(row, dict) and {"t", "v", "d", "id"} <= set(row)
                for row in body)
    )


def _shape_windsolar(body) -> bool:
    return isinstance(body, dict) and {"instance", "RefId", "MktDay"} <= set(body)


ENDPOINTS = [
    {"key": "FuelMix", "path": "/api/FuelMix",
     "shape": _shape_fuelmix, "ref_path": ("RefId",)},
    {"key": "RealTimeTotalLoad", "path": "/api/RealTimeTotalLoad",
     "shape": _shape_load, "ref_path": ("LoadInfo", "RefId")},
    {"key": "Snapshot", "path": "/api/Snapshot",
     "shape": _shape_snapshot, "ref_path": None},
    {"key": "WindSolar", "path": "/api/WindSolar/GetCombined",
     "shape": _shape_windsolar, "ref_path": ("RefId",)},
]


# --- configuration ----------------------------------------------------------

def base_url() -> str:
    """MISO's base URL, or a stub. Trailing slashes stripped so paths join cleanly."""
    return (os.environ.get("MISO_API_BASE") or DEFAULT_BASE_URL).rstrip("/")


def raw_dir() -> Path:
    """Where payloads are written.

    Anchored to the repo root derived from this file, never to the working
    directory, so `python -m backend.poller` writes the same place from
    anywhere. This file is backend/poller/core.py, so the repo root is three
    parents up - one more than backend/config.py needs.
    """
    override = os.environ.get("MISO_RAW_DIR")
    if override:
        # Resolved, because specification 6.3 defines `raw_dir` in the status
        # file as the absolute directory actually written. A relative override
        # recorded verbatim as "relraw" identifies nothing, which defeats the
        # only reason the field exists. The default below is already absolute.
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent.parent / "data" / "raw"


def poll_seconds() -> int:
    """Seconds between cycles, clamped to a sane range."""
    raw = os.environ.get("MISO_POLL_SECONDS")
    if not raw:
        return DEFAULT_POLL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        log.warning("MISO_POLL_SECONDS=%r is not an integer, using %d",
                    raw, DEFAULT_POLL_SECONDS)
        return DEFAULT_POLL_SECONDS
    return max(MIN_POLL_SECONDS, min(MAX_POLL_SECONDS, value))


def poller_enabled() -> bool:
    """Whether FastAPI should register the scheduled job.

    Every common spelling of "off" counts, because `MISO_POLLER_ENABLED=false`
    meaning "on" would be a trap.
    """
    raw = os.environ.get("MISO_POLLER_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def using_stub() -> bool:
    """True only when the base URL provably names this machine.

    A local stub has no rate limit and guarding it would silently cancel
    MISO_POLL_SECONDS below 60, so the bypass has to exist. But it has to
    turn on the destination, not the spelling. Comparing the base string to
    DEFAULT_BASE_URL answered the wrong question: `http://`, a capitalized
    host, an explicit `:443` and a trailing dot are four different strings
    that all reach MISO, and each one of them switched the guard off while
    every request still went to an organization that IP-bans.

    So the test is inverted. Loopback bypasses; anything else is guarded.
    A hostname nobody anticipated - a typo, a proxy, a new spelling - then
    fails safe, at the price of one guarded stub for anyone who binds a stub
    to a non-loopback address.

    No DNS. This decides in front of every fetch, and a resolver that maps
    some name onto 127.0.0.1 is not worth a lookup in that path.
    """
    host = urlsplit(base_url()).hostname
    if host is None:
        return False
    # The root label is legal in a hostname and changes nothing about where
    # the request lands, here or at MISO.
    host = host.rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def now() -> datetime:
    """Our wall clock, offset-aware.

    This is the team's local time, not MISO's. MISO stamps its data in fixed
    EST year-round, so from March to November our timestamps and MISO's RefId
    are an hour apart and both are correct.
    """
    return datetime.now(TIMEZONE)


# --- atomic writes ----------------------------------------------------------

def write_atomic(path: Path, payload: bytes) -> None:
    """Write bytes so a reader sees the old file or the new one, never a partial.

    The temp file gets a unique name from mkstemp rather than a fixed
    "<file>.tmp". Two writers sharing one temp path can truncate each other
    mid-write and publish a partial file as though it were complete;
    os.replace being atomic does not save you from that.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        # os.fdopen takes ownership of the descriptor only once it returns.
        # If it raises - MemoryError is the realistic way - nothing else will
        # ever close fd, and the process leaks one descriptor per call.
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates 0600; these files are a cross-lane interface.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def sweep_stale_tmp(directory: Path) -> None:
    """Delete leftover .tmp files older than 10 minutes.

    Age-filtered on purpose. An unconditional sweep would delete a concurrent
    writer's in-flight temp file and break its os.replace, trading one hazard
    for another. Ten minutes is far longer than a cycle and far shorter than
    the gap between debugging sessions.
    """
    cutoff = time_now() - STALE_TMP_SECONDS
    for leftover in directory.glob("*.tmp"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
                log.info("swept stale temp file %s", leftover.name)
        except OSError:
            pass


def time_now() -> float:
    """Wall-clock seconds. Split out so tests can move it."""
    import time

    return time.time()


# --- status file ------------------------------------------------------------

# The shape every endpoint entry is built from, and the only keys carried
# forward by _usable_entry. `outcome` is deliberately absent: it describes one
# cycle, so it must be written fresh each cycle and never inherited. Leaving it
# out means a stale value cannot survive a merge even if a future code path
# forgets to set it.
INITIAL_ENTRY = {
    "last_attempt": None,
    "last_success": None,
    "consecutive_failures": 0,
    "last_error": None,
    "http_status": None,
    "bytes": None,
    "ref_id": None,
    "ref_id_changed_at": None,
}


def status_path(directory: Path) -> Path:
    return directory / "_status.json"


def read_status(directory: Path) -> dict:
    """Load _status.json, tolerating every way it can be unusable.

    Missing, unparseable, or valid JSON that is not an object with an
    `endpoints` object all mean the same thing: start over. A file holding
    `[]` or `null` parses fine and is still no use.

    RecursionError is in the tuple because a deeply nested file is exactly
    the corrupt one an operator runs --status to inspect, and json raises it
    rather than ValueError. Letting it escape killed both --once and the
    diagnostic meant to explain why.
    """
    try:
        data = json.loads(status_path(directory).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as e:
        log.warning("status file unreadable (%s), starting fresh", e)
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("endpoints"), dict):
        log.warning("status file is not shaped as expected, starting fresh")
        return {}
    return data


def _usable_entry(entry) -> dict:
    """Return a previous endpoint entry if it is safe to build on, else a fresh one.

    A hand-edited "consecutive_failures": "3" would raise on += 1, so entries
    are type-checked rather than trusted.
    """
    if not isinstance(entry, dict):
        return dict(INITIAL_ENTRY)
    failures = entry.get("consecutive_failures")
    # bool before int: True is an instance of int, so a hand-edited
    # "consecutive_failures": true passed this gate and counted up from 1.
    # Specification 6.1 asks for a non-negative integer, and true is not one.
    if isinstance(failures, bool) or not isinstance(failures, int):
        return dict(INITIAL_ENTRY)
    if failures < 0:
        return dict(INITIAL_ENTRY)
    for field in ("last_attempt", "last_success", "ref_id_changed_at"):
        value = entry.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            return dict(INITIAL_ENTRY)
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return dict(INITIAL_ENTRY)
    merged = dict(INITIAL_ENTRY)
    merged.update({k: v for k, v in entry.items() if k in INITIAL_ENTRY})
    return merged


# --- fetching one endpoint --------------------------------------------------

def extract_ref_id(body, ref_path):
    """Dig a RefId out of a payload. None when the endpoint has none (Snapshot)."""
    if ref_path is None:
        return None
    value = body
    for key in ref_path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, str) else None


def _fetch(endpoint: dict, url: str) -> dict:
    """One request. Returns a result dict; never raises for a MISO-side problem.

    Every failure lands in the closed vocabulary of last_error values. Raw
    exception text is deliberately not used - requests exceptions are
    multi-line and embed the full URL, and this string reaches a file the RAG
    lane may render.
    """
    try:
        response = requests.get(url, timeout=TIMEOUT, headers=HEADERS,
                                allow_redirects=False)
    except requests.exceptions.MissingSchema:
        return {"ok": False, "error": "bad base url"}
    except requests.exceptions.InvalidURL:
        return {"ok": False, "error": "bad base url"}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "timeout"}
    except (requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError):
        return {"ok": False, "error": "truncated response"}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "connection error"}
    except requests.exceptions.RequestException:
        log.warning("unexpected request failure for %s", endpoint["key"], exc_info=True)
        return {"ok": False, "error": "internal error"}

    size = len(response.content)
    status = response.status_code

    if 300 <= status < 400:
        return {"ok": False, "error": f"redirect {status}",
                "http_status": status, "bytes": size}
    if status != 200:
        return {"ok": False, "error": f"HTTP {status}",
                "http_status": status, "bytes": size}

    try:
        body = json.loads(response.content)
    except (ValueError, RecursionError):
        # A body nested thousands of levels deep raises RecursionError, not
        # ValueError. It is still a payload we cannot parse, and reporting it
        # as "internal error" pointed the operator at our code instead of at
        # what the server sent.
        return {"ok": False, "error": "invalid JSON",
                "http_status": status, "bytes": size}

    if not endpoint["shape"](body):
        return {"ok": False, "error": "shape check failed",
                "http_status": status, "bytes": size}

    ref_id = extract_ref_id(body, endpoint["ref_path"])
    if endpoint["ref_path"] is not None and ref_id is None:
        return {"ok": False, "error": "missing ref_id",
                "http_status": status, "bytes": size}

    return {"ok": True, "http_status": status, "bytes": size,
            "ref_id": ref_id, "content": response.content}


# --- one endpoint, end to end -----------------------------------------------

def _poll_endpoint(endpoint: dict, url: str, directory: Path,
                   stub: bool) -> dict:
    """Claim, fetch, validate and write one endpoint. Returns what it observed.

    Every path returns an `outcome`, because that is what the cycle's exit
    code is read from. Lifted out of poll_once so the caller can wrap one
    endpoint's work in a single except clause without that clause swallowing
    the loop itself.

    Nothing here reads the stored status. The observation describes only this
    cycle, and _merge_observation folds it onto the stored entry later, under
    the status lock. That separation is what keeps a network request out of
    that lock while still making the read-merge-write one critical section.
    """
    key = endpoint["key"]

    if not stub and not guard.claim(url):
        # last_error is deliberately left as it was. An endpoint that has
        # failed five times with HTTP 503 and is then guard-skipped must still
        # report the 503: the operator reading this file is debugging that
        # incident, and "rate guard" would send them after the wrong thing.
        # The skip is not lost - outcome records it, and guard.claim logged it.
        return {"outcome": "skipped"}

    result = _fetch(endpoint, url)
    observed = {
        "outcome": "failed",
        "last_attempt": now().isoformat(),
        "http_status": result.get("http_status"),
        "bytes": result.get("bytes"),
        "last_error": result.get("error"),
        "ref_id": None,
    }

    if not result["ok"]:
        log.warning("%s failed: %s", key, result["error"])
        return observed

    try:
        write_atomic(directory / f"{key}.json", result["content"])
    except OSError as e:
        observed["last_error"] = "internal error"
        log.error("%s: could not write payload (%s)", key, e)
        return observed

    observed["outcome"] = "ok"
    observed["last_error"] = None
    observed["ref_id"] = result["ref_id"]
    log.info("%s ok (%s bytes)", key, result["bytes"])
    return observed


def _internal_error_observation() -> dict:
    """What an endpoint that raised out of _poll_endpoint leaves behind."""
    return {"outcome": "failed", "last_attempt": now().isoformat(),
            "http_status": None, "bytes": None,
            "last_error": "internal error", "ref_id": None}


def _merge_observation(entry: dict, observed: dict) -> None:
    """Fold one cycle's observation onto the stored entry. Mutates `entry`.

    `consecutive_failures`, `last_success`, `ref_id` and `ref_id_changed_at`
    are carried forward from `entry`, so `entry` must be the version read
    under the status lock and not the one that was current before the fetch.
    """
    entry["outcome"] = observed["outcome"]
    if observed["outcome"] == "skipped":
        return

    entry["last_attempt"] = observed["last_attempt"]
    entry["http_status"] = observed["http_status"]
    entry["bytes"] = observed["bytes"]

    if observed["outcome"] == "failed":
        entry["consecutive_failures"] += 1
        entry["last_error"] = observed["last_error"]
        return

    if observed["ref_id"] != entry.get("ref_id"):
        entry["ref_id_changed_at"] = observed["last_attempt"]
    entry["ref_id"] = observed["ref_id"]
    entry["consecutive_failures"] = 0
    entry["last_error"] = None
    entry["last_success"] = observed["last_attempt"]


# --- pruning a removed endpoint ---------------------------------------------

# What an endpoint key may look like before we will build a filename from it.
# The status file is editable by hand, so a key is not trusted to be a safe
# path component just because it was in there.
SAFE_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _prune_payloads(directory: Path, previous_endpoints: dict,
                    current: dict) -> None:
    """Delete the payload file of any endpoint that has left the table.

    Specification 6.1: a pruned status entry whose .json is still sitting
    beside it leaves the RAG lane a file that parses, passes a shape check,
    looks live, and has no provenance anywhere.

    Only the exact filename of a key we just pruned is unlinked - never a
    glob, never _status.json, and never a key whose text could name something
    else in the directory.
    """
    # Compared case-foldedly because this machine's filesystem is. A Path
    # equality test is case-sensitive wherever it runs, so on APFS a status
    # entry keyed "_STATUS" named a different Path, compared unequal, and
    # unlinked the real status file.
    status_key = status_path(directory).stem.casefold()
    for key in previous_endpoints:
        if key in current:
            continue
        if not isinstance(key, str) or not SAFE_KEY.fullmatch(key):
            log.warning("pruned an unrecognized endpoint key %r, "
                        "leaving any file of its name alone", key)
            continue
        if key.casefold() == status_key:
            log.warning("pruned endpoint key %r names the status file, "
                        "leaving it alone", key)
            continue
        target = directory / f"{key}.json"
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError as e:
            log.warning("pruned %s but could not delete %s (%s)",
                        key, target.name, e)
            continue
        log.info("pruned endpoint %s, deleted %s", key, target.name)


# --- one full cycle ---------------------------------------------------------

def poll_once() -> dict:
    """Fetch all four endpoints, validate, write files, update the status file.

    Returns the status dict that was written. If every endpoint was skipped by
    the rate guard, nothing is fetched or written and the previously stored
    status is returned with "skipped": True.

    Raises OSError if data/raw/ cannot be created or _status.json cannot be
    written. Network and MISO failures never raise; they are recorded.
    """
    directory = raw_dir()
    directory.mkdir(parents=True, exist_ok=True)
    sweep_stale_tmp(directory)

    base = base_url()
    stub = using_stub()
    if base != DEFAULT_BASE_URL:
        # Specification 8.6 wants a non-default base to be loud every cycle.
        # Whether the guard is on is a separate sentence, because a
        # non-default base that is not loopback still reaches MISO and is
        # still guarded, and an operator skimming the log must be able to
        # tell the two apart at a glance.
        if stub:
            log.warning("MISO_API_BASE is %s - loopback, RATE GUARD BYPASSED",
                        base)
        else:
            log.warning("MISO_API_BASE is %s - not loopback, rate guard ACTIVE",
                        base)
    if os.environ.get("MISO_RAW_DIR"):
        log.warning("MISO_RAW_DIR is set, writing to %s", directory)

    started = now()

    # Every network request happens here, with no lock of ours held. The
    # status file is not read yet: anything read before the fetches is stale
    # by the time they finish, and merging onto a stale entry is how two
    # concurrent cycles each counted one failure where there were two.
    observations = {}
    for endpoint in ENDPOINTS:
        key = endpoint["key"]
        try:
            observations[key] = _poll_endpoint(
                endpoint, base + endpoint["path"], directory, stub)
        except Exception:
            # Specification section 7: any exception in one endpoint is
            # recorded and the other three still run. _fetch now names the
            # failures it can foresee, so anything arriving here is a bug in
            # our code and is reported as one.
            log.exception("%s: unexpected failure", key)
            observations[key] = _internal_error_observation()

    # Read from this cycle's outcomes, not from a counter: "nothing was
    # attempted" is exit 2 and "nothing succeeded" is exit 1, and a counter
    # incremented mid-loop cannot say which one an internal error belongs to.
    if all(o["outcome"] == "skipped" for o in observations.values()):
        log.warning("every endpoint was skipped by the rate guard")
        # Nothing was fetched, so nothing is rewritten and no lock is needed:
        # the stored file still describes the last cycle that ran, and its
        # payloads stay beside it unpruned.
        previous = read_status(directory)
        skipped = dict(previous) if previous else {
            "endpoints": {e["key"]: dict(INITIAL_ENTRY, path=e["path"],
                                         outcome="skipped")
                          for e in ENDPOINTS}}
        skipped["skipped"] = True
        return skipped

    # Read, merge, prune and write are one critical section. Unserialized,
    # six concurrent cycles all read the same consecutive_failures and all
    # wrote it plus one, and a partial overlap let the later writer drop the
    # earlier one's last_success and ref_id. The lock file is the raw
    # directory's, not the rate guard's, so a status write never waits behind
    # a request lease - and no fetch is inside it, every request above has
    # already returned.
    with guard.file_lock(status_path(directory)):
        previous_endpoints = read_status(directory).get("endpoints", {})
        results = {}
        for endpoint in ENDPOINTS:
            key = endpoint["key"]
            entry = _usable_entry(previous_endpoints.get(key))
            entry["path"] = endpoint["path"]
            _merge_observation(entry, observations[key])
            results[key] = entry

        _prune_payloads(directory, previous_endpoints, results)

        status = {
            "base_url": base,
            "raw_dir": str(directory),
            "cycle_started_at": started.isoformat(),
            "cycle_finished_at": now().isoformat(),
            "endpoints": results,
        }
        write_atomic(status_path(directory),
                     json.dumps(status, indent=2).encode())
    return status


def succeeded_count(status: dict) -> int:
    """How many endpoints succeeded in the cycle this status describes.

    Read from `outcome`, which records what happened this cycle. No
    carried-forward field can answer this question: last_success survives a
    total outage, and consecutive_failures is left untouched by a rate-guard
    skip, so a skipped endpoint kept the 0 an earlier success had put there
    and a cycle of three 503s plus one skip reported a success.
    """
    endpoints = status.get("endpoints", {})
    return sum(1 for e in endpoints.values()
               if isinstance(e, dict) and e.get("outcome") == "ok")
