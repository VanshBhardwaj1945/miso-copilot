"""Fetch four MISO public JSON endpoints and write them verbatim to data/raw/.

This is the whole job of the ingestion lane. It does not interpret what it
downloads: the bytes MISO returns are the bytes written to disk. The RAG lane
will read those files, and everything downstream - prose, embedding, Chroma,
retrieval - belongs to it. Nothing reads them yet: backend/rag/ holds
a README and no code.

Two deliberate exceptions to "does not interpret": a shape gate that rejects
HTML error pages and empty payloads, and a `ref_id` lifted out of each payload
so that a frozen feed is visible in the status file.

Every endpoint entry in `_status.json` carries an `outcome` of "ok", "failed"
or "skipped". It is the only field describing THIS cycle and nothing else:
`consecutive_failures`, `last_success` and `ref_id` are carried forward across
cycles on purpose, so none of them can tell a cycle where everything failed
from one where a rate-guard skip inherited a zero from an earlier success.
Exit codes are read from `outcome` for exactly that reason.

Entry point is poll_once().
"""

import contextlib
import ipaddress
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
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

def _shape_fuelmix(body: object) -> bool:
    return isinstance(body, dict) and {"RefId", "TotalMW", "Fuel"} <= set(body)


def _shape_load(body: object) -> bool:
    return isinstance(body, dict) and "LoadInfo" in body


def _shape_snapshot(body: object) -> bool:
    """Non-empty array whose every row carries the four fields MISO publishes.

    The per-row check earns its keep: Snapshot is the one endpoint with no
    RefId, so the ref_id gate does not protect it, and it carries Current
    Demand and Marginal Energy Cost - the numbers a judge asks for first.
    Without this, `[{}]` would overwrite good data and record success.
    """
    return (
        isinstance(body, list)
        and len(body) > 0
        and all(isinstance(row, dict) and {"t", "v", "d", "id"} <= set(row)
                for row in body)
    )


def _shape_windsolar(body: object) -> bool:
    return isinstance(body, dict) and {"instance", "RefId", "MktDay"} <= set(body)


class Endpoint(NamedTuple):
    """One polled link: where it lives, what it must look like, its RefId.

    A named tuple rather than a dict because every field is read by name in
    five places, and a typo in a string key is a KeyError at poll time rather
    than an error a checker can see.
    """

    key: str
    path: str
    shape: Callable[[object], bool]
    ref_path: tuple[str, ...] | None   # None for Snapshot - it has no RefId


# Snapshot is exempt from the ref_id gate and has no frozen-feed signal,
# because it carries no RefId at any level.
ENDPOINTS = [
    Endpoint("FuelMix", "/api/FuelMix",
             _shape_fuelmix, ("RefId",)),
    Endpoint("RealTimeTotalLoad", "/api/RealTimeTotalLoad",
             _shape_load, ("LoadInfo", "RefId")),
    Endpoint("Snapshot", "/api/Snapshot",
             _shape_snapshot, None),
    Endpoint("WindSolar", "/api/WindSolar/GetCombined",
             _shape_windsolar, ("RefId",)),
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
        # Resolved, because `raw_dir` in the status
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


def base_is_loopback(base: str) -> bool:
    """True only when `base` provably names this machine.

    This is what the rate-guard bypass turns on. A local stub has no rate
    limit and guarding it would silently cancel MISO_POLL_SECONDS below 60,
    so the bypass has to exist - but it has to key on the destination, not
    the spelling. Comparing the base string to DEFAULT_BASE_URL answered the
    wrong question: `http://`, a capitalized host, an explicit `:443` and a
    trailing dot are four different strings that all reach MISO, and each one
    of them switched the guard off while every request still went to an
    organization that IP-bans.

    So the test is inverted. Loopback bypasses; anything else is guarded. A
    hostname nobody anticipated - a typo, a proxy, a new spelling - then
    fails safe, at the price of one guarded stub for anyone who binds a stub
    to a non-loopback address.

    No DNS. This decides in front of every fetch, and a resolver that maps
    some name onto 127.0.0.1 is not worth a lookup in that path.
    """
    host = urlsplit(base).hostname
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

    The ".tmp" suffix is the pattern sweep_stale_tmp globs for. Change one
    without the other and leftovers stop being cleaned up.
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
        if tmp is not None:
            # suppress rather than check-then-act: a file that vanishes
            # between the check and the unlink raises from the finally and
            # masks whatever exception sent us here.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)


def sweep_stale_tmp(directory: Path) -> None:
    """Delete leftover .tmp files older than STALE_TMP_SECONDS.

    The age filter is the point. An unconditional sweep would delete a
    concurrent writer's in-flight temp file and break its os.replace, trading
    one hazard for another. STALE_TMP_SECONDS is far longer than a cycle and
    far shorter than the gap between debugging sessions.
    """
    cutoff = time.time() - STALE_TMP_SECONDS
    for leftover in directory.glob("*.tmp"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
                log.info("swept stale temp file %s", leftover.name)
        except OSError:
            pass


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
    """Where the status file for a raw directory lives."""
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


def _entry_is_usable(entry) -> bool:
    """Whether a stored endpoint entry is safe to build this cycle on.

    A hand-edited "consecutive_failures": "3" would raise on += 1, so entries
    are type-checked rather than trusted.
    """
    if not isinstance(entry, dict):
        return False
    failures = entry.get("consecutive_failures")
    # bool before int: True is an instance of int, so a hand-edited
    # "consecutive_failures": true passed this gate and counted up from 1.
    # Specification 6.1 asks for a non-negative integer, and true is not one.
    if isinstance(failures, bool) or not isinstance(failures, int):
        return False
    if failures < 0:
        return False
    for field in ("last_attempt", "last_success", "ref_id_changed_at"):
        value = entry.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return False
    return True


def _usable_entry(entry) -> dict:
    """The stored entry if it is usable, else a fresh one. Never the caller's."""
    if not _entry_is_usable(entry):
        return dict(INITIAL_ENTRY)
    merged = dict(INITIAL_ENTRY)
    merged.update({k: v for k, v in entry.items() if k in INITIAL_ENTRY})
    return merged


# --- fetching one endpoint --------------------------------------------------

def extract_ref_id(body: object,
                   ref_path: tuple[str, ...] | None) -> str | None:
    """Dig a RefId out of a payload. None when the endpoint has none (Snapshot)."""
    if ref_path is None:
        return None
    value = body
    for key in ref_path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, str) else None


def _fetch_failure(error: str, status: int | None = None,
                   size: int | None = None) -> dict:
    """A _fetch result for a request that produced no usable payload."""
    return {"ok": False, "error": error, "http_status": status,
            "bytes": size, "ref_id": None, "content": None}


def _validate(endpoint: Endpoint, content: bytes, status: int,
              size: int) -> dict:
    """Parse and gate one 200 body. Returns a _fetch result dict.

    This is the lane's entire interpretation budget: parse, shape gate,
    ref_id. Pure, so it can be exercised with a bytes literal and no socket.
    """
    try:
        body = json.loads(content)
    except (ValueError, RecursionError):
        # A body nested thousands of levels deep raises RecursionError, not
        # ValueError. It is still a payload we cannot parse, and reporting it
        # as "internal error" pointed the operator at our code instead of at
        # what the server sent.
        return _fetch_failure("invalid JSON", status, size)

    if not endpoint.shape(body):
        return _fetch_failure("shape check failed", status, size)

    ref_id = extract_ref_id(body, endpoint.ref_path)
    if endpoint.ref_path is not None and ref_id is None:
        return _fetch_failure("missing ref_id", status, size)

    return {"ok": True, "error": None, "http_status": status, "bytes": size,
            "ref_id": ref_id, "content": content}


def _fetch(endpoint: Endpoint, url: str) -> dict:
    """One request. Returns a result dict; never raises for a MISO-side problem.

    Every failure lands in the closed vocabulary of last_error values. Raw
    exception text is deliberately not used - requests exceptions are
    multi-line and embed the full URL, and this string reaches a file the RAG
    lane may render.

    Every path returns the same six keys - ok, error, http_status, bytes,
    ref_id, content - with None where a value does not apply, so the caller
    subscripts rather than guesses which ones are present.
    """
    try:
        response = requests.get(url, timeout=TIMEOUT, headers=HEADERS,
                                allow_redirects=False)
    except (requests.exceptions.MissingSchema,
            requests.exceptions.InvalidURL):
        return _fetch_failure("bad base url")
    except requests.exceptions.Timeout:
        return _fetch_failure("timeout")
    except (requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError):
        return _fetch_failure("truncated response")
    except requests.exceptions.ConnectionError:
        return _fetch_failure("connection error")
    except requests.exceptions.RequestException:
        log.warning("unexpected request failure for %s", endpoint.key,
                    exc_info=True)
        return _fetch_failure("internal error")

    size = len(response.content)
    status = response.status_code

    if 300 <= status < 400:
        return _fetch_failure(f"redirect {status}", status, size)
    if status != 200:
        return _fetch_failure(f"HTTP {status}", status, size)

    return _validate(endpoint, response.content, status, size)


# --- one endpoint, end to end -----------------------------------------------

def _skipped_observation() -> dict:
    """What a rate-guard skip leaves behind. Nothing was attempted.

    last_error is None here rather than a rate-guard message, and
    _merge_observation stops at `outcome`, so the stored entry keeps the
    error it already had. An endpoint that has failed five times with HTTP
    503 and is then guard-skipped must still report the 503: the operator
    reading the file is debugging that incident, and "rate guard" would send
    them after the wrong thing. The skip is not lost - outcome records it,
    and guard.claim logged it.
    """
    return {"outcome": "skipped", "last_attempt": None, "http_status": None,
            "bytes": None, "last_error": None, "ref_id": None}


def _internal_error_observation() -> dict:
    """What an endpoint that raised out of _poll_endpoint leaves behind."""
    return {"outcome": "failed", "last_attempt": now().isoformat(),
            "http_status": None, "bytes": None,
            "last_error": "internal error", "ref_id": None}


def _poll_endpoint(endpoint: Endpoint, url: str, directory: Path,
                   bypass_guard: bool) -> dict:
    """Claim, fetch, validate and write one endpoint. Returns what it observed.

    Every path returns the same six keys, `outcome` among them, because that
    is what the cycle's exit code is read from. Lifted out of poll_once so
    the caller can wrap one endpoint's work in a single except clause without
    that clause swallowing the loop itself.

    Nothing here reads the stored status. The observation describes only this
    cycle, and _merge_observation folds it onto the stored entry later, under
    the status lock. That separation is what keeps a network request out of
    that lock while still making the read-merge-write one critical section.
    """
    key = endpoint.key

    if not bypass_guard and not guard.claim(url):
        return _skipped_observation()

    result = _fetch(endpoint, url)
    observed = {
        "outcome": "failed",
        "last_attempt": now().isoformat(),
        "http_status": result["http_status"],
        "bytes": result["bytes"],
        "last_error": result["error"],
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


def _merge_observation(entry: dict, observed: dict) -> None:
    """Fold one cycle's observation onto the stored entry. Mutates `entry`.

    A skipped cycle stops at `outcome`: nothing was attempted, so every other
    field keeps whatever the last real attempt left there.

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

def _log_configuration(base: str, bypass_guard: bool, directory: Path) -> None:
    """Announce a non-default base and raw directory, once per cycle.

    Specification 8.6 wants a non-default base to be loud every cycle.
    Whether the guard is on is a separate sentence, because a non-default
    base that is not loopback still reaches MISO and is still guarded, and an
    operator skimming the log must be able to tell the two apart at a glance.
    """
    if base != DEFAULT_BASE_URL:
        if bypass_guard:
            log.warning("MISO_API_BASE is %s - loopback, RATE GUARD BYPASSED",
                        base)
        else:
            log.warning("MISO_API_BASE is %s - not loopback, rate guard ACTIVE",
                        base)
    if os.environ.get("MISO_RAW_DIR"):
        log.warning("MISO_RAW_DIR is set, writing to %s", directory)


def _all_skipped_status(directory: Path) -> dict:
    """The status to return when the rate guard skipped every endpoint.

    Nothing was fetched, so nothing is rewritten and no lock is needed: the
    stored file still describes the last cycle that ran, and its payloads
    stay beside it unpruned. With no stored file there is nothing to describe,
    so a fresh entry per endpoint is synthesized to say so.
    """
    previous = read_status(directory)
    unchanged = dict(previous) if previous else {
        "endpoints": {endpoint.key: dict(INITIAL_ENTRY, path=endpoint.path,
                                         outcome="skipped")
                      for endpoint in ENDPOINTS}}
    unchanged["skipped"] = True
    return unchanged


def _write_status(directory: Path, observations: dict, base: str,
                  started: datetime) -> dict:
    """Merge this cycle onto the stored status and write it. Returns what it wrote.

    Read, merge, prune and write are one critical section. Unserialized, six
    concurrent cycles all read the same consecutive_failures and all wrote it
    plus one, and a partial overlap let the later writer drop the earlier
    one's last_success and ref_id. The lock file is the raw directory's, not
    the rate guard's, so a status write never waits behind a request lease -
    and no fetch is inside it, every request has already returned.
    """
    with guard.file_lock(status_path(directory)):
        previous_endpoints = read_status(directory).get("endpoints", {})
        results = {}
        for endpoint in ENDPOINTS:
            key = endpoint.key
            entry = _usable_entry(previous_endpoints.get(key))
            entry["path"] = endpoint.path
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
    bypass_guard = base_is_loopback(base)
    _log_configuration(base, bypass_guard, directory)

    started = now()

    # Every network request happens here, with no lock of ours held. The
    # status file is not read yet: anything read before the fetches is stale
    # by the time they finish, and merging onto a stale entry is how two
    # concurrent cycles each counted one failure where there were two.
    observations = {}
    for endpoint in ENDPOINTS:
        key = endpoint.key
        try:
            observations[key] = _poll_endpoint(
                endpoint, base + endpoint.path, directory, bypass_guard)
        except Exception:
            # Specification section 7: any exception in one endpoint is
            # recorded and the other three still run. _fetch names the
            # failures it can foresee, so anything arriving here is a bug in
            # our code and is reported as one.
            log.exception("%s: unexpected failure", key)
            observations[key] = _internal_error_observation()

    # Read from this cycle's outcomes, not from a counter: "nothing was
    # attempted" is exit 2 and "nothing succeeded" is exit 1, and a counter
    # incremented mid-loop cannot say which one an internal error belongs to.
    if all(o["outcome"] == "skipped" for o in observations.values()):
        log.warning("every endpoint was skipped by the rate guard")
        return _all_skipped_status(directory)

    return _write_status(directory, observations, base, started)


def succeeded_count(status: dict) -> int:
    """How many endpoints succeeded in the cycle this status describes.

    Read from `outcome`, the one field that describes this cycle. No
    carried-forward field can answer it: last_success survives a total
    outage, and consecutive_failures is left untouched by a rate-guard skip.
    """
    endpoints = status.get("endpoints", {})
    return sum(1 for e in endpoints.values()
               if isinstance(e, dict) and e.get("outcome") == "ok")
