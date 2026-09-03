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

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
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
        return Path(override).expanduser()
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
    """True when pointed somewhere other than MISO.

    The rate guard applies only to MISO. A local stub has no rate limit, and
    guarding it would silently cancel MISO_POLL_SECONDS below 60.
    """
    return base_url() != DEFAULT_BASE_URL


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
        with os.fdopen(fd, "wb") as handle:
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
    """
    try:
        data = json.loads(status_path(directory).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
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
    if not isinstance(entry.get("consecutive_failures"), int):
        return dict(INITIAL_ENTRY)
    if entry["consecutive_failures"] < 0:
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
    except ValueError:
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

def _poll_endpoint(endpoint: dict, url: str, entry: dict, directory: Path,
                   stub: bool) -> None:
    """Claim, fetch, validate and write one endpoint. Mutates `entry` in place.

    Every path through this function sets `entry["outcome"]`, because that is
    what the cycle's exit code is read from. Lifted out of poll_once so the
    caller can wrap one endpoint's work in a single except clause without
    that clause swallowing the loop itself.
    """
    key = endpoint["key"]

    if not stub and not guard.claim(url):
        # last_error is deliberately left as it was. An endpoint that has
        # failed five times with HTTP 503 and is then guard-skipped must still
        # report the 503: the operator reading this file is debugging that
        # incident, and "rate guard" would send them after the wrong thing.
        # The skip is not lost - outcome records it, and guard.claim logged it.
        entry["outcome"] = "skipped"
        return

    result = _fetch(endpoint, url)
    moment = now()
    entry["last_attempt"] = moment.isoformat()
    entry["http_status"] = result.get("http_status")
    entry["bytes"] = result.get("bytes")

    if not result["ok"]:
        entry["outcome"] = "failed"
        entry["consecutive_failures"] += 1
        entry["last_error"] = result["error"]
        log.warning("%s failed: %s", key, result["error"])
        return

    try:
        write_atomic(directory / f"{key}.json", result["content"])
    except OSError as e:
        entry["outcome"] = "failed"
        entry["consecutive_failures"] += 1
        entry["last_error"] = "internal error"
        log.error("%s: could not write payload (%s)", key, e)
        return

    if result["ref_id"] != entry.get("ref_id"):
        entry["ref_id_changed_at"] = moment.isoformat()
    entry["ref_id"] = result["ref_id"]
    entry["outcome"] = "ok"
    entry["consecutive_failures"] = 0
    entry["last_error"] = None
    entry["last_success"] = moment.isoformat()
    log.info("%s ok (%s bytes)", key, result["bytes"])


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
    for key in previous_endpoints:
        if key in current:
            continue
        if not isinstance(key, str) or not SAFE_KEY.fullmatch(key):
            log.warning("pruned an unrecognized endpoint key %r, "
                        "leaving any file of its name alone", key)
            continue
        target = directory / f"{key}.json"
        if target == status_path(directory):
            continue
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
    if stub:
        log.warning("MISO_API_BASE is %s, not MISO - rate guard bypassed", base)
    if os.environ.get("MISO_RAW_DIR"):
        log.warning("MISO_RAW_DIR is set, writing to %s", directory)

    previous = read_status(directory)
    previous_endpoints = previous.get("endpoints", {})
    started = now()

    results = {}
    for endpoint in ENDPOINTS:
        key = endpoint["key"]
        entry = _usable_entry(previous_endpoints.get(key))
        entry["path"] = endpoint["path"]
        try:
            _poll_endpoint(endpoint, base + endpoint["path"], entry,
                           directory, stub)
        except Exception:
            # Specification section 7: any exception in one endpoint is
            # recorded and the other three still run. A RecursionError out of
            # json.loads on a deeply nested body is the case that proved this
            # necessary - it is not a ValueError, so it escaped _fetch, killed
            # the cycle, and _status.json was never written at all.
            log.exception("%s: unexpected failure", key)
            entry["outcome"] = "failed"
            entry["consecutive_failures"] += 1
            entry["last_error"] = "internal error"
        results[key] = entry

    # Read from this cycle's outcomes, not from a counter: "nothing was
    # attempted" is exit 2 and "nothing succeeded" is exit 1, and a counter
    # incremented mid-loop cannot say which one an internal error belongs to.
    if all(entry.get("outcome") == "skipped" for entry in results.values()):
        log.warning("every endpoint was skipped by the rate guard")
        skipped = dict(previous) if previous else {"endpoints": results}
        skipped["skipped"] = True
        return skipped

    # Only when the file is actually being rewritten - a fully skipped cycle
    # leaves the old entries in place, so their payloads stay too.
    _prune_payloads(directory, previous_endpoints, results)

    status = {
        "base_url": base,
        "raw_dir": str(directory),
        "cycle_started_at": started.isoformat(),
        "cycle_finished_at": now().isoformat(),
        "endpoints": results,
    }
    write_atomic(status_path(directory), json.dumps(status, indent=2).encode())
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
