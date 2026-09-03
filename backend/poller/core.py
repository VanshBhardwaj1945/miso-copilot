"""Fetch four MISO public JSON endpoints and write them verbatim to data/raw/.

This is the whole job of the ingestion lane. It does not interpret what it
downloads: the bytes MISO returns are the bytes written to disk. The RAG lane
reads those files and everything downstream - prose, embedding, Chroma,
retrieval - belongs to it.

Two deliberate exceptions to "does not interpret": a shape gate that rejects
HTML error pages and empty payloads, and a `ref_id` lifted out of each payload
so that a frozen feed is visible in the status file.

Entry point is poll_once(). See the specification, sections 5 and 6.
"""

import json
import logging
import os
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
    attempted = 0
    for endpoint in ENDPOINTS:
        key = endpoint["key"]
        url = base + endpoint["path"]
        entry = _usable_entry(previous_endpoints.get(key))
        entry["path"] = endpoint["path"]

        if stub:
            outcome = _fetch(endpoint, url)
            attempted += 1
        else:
            with guard.claim(url, now()) as allowed:
                if not allowed:
                    entry["last_error"] = "rate guard"
                    results[key] = entry
                    continue
                outcome = _fetch(endpoint, url)
                attempted += 1

        moment = now()
        entry["last_attempt"] = moment.isoformat()
        entry["http_status"] = outcome.get("http_status")
        entry["bytes"] = outcome.get("bytes")

        if not outcome["ok"]:
            entry["consecutive_failures"] += 1
            entry["last_error"] = outcome["error"]
            log.warning("%s failed: %s", key, outcome["error"])
            results[key] = entry
            continue

        try:
            write_atomic(directory / f"{key}.json", outcome["content"])
        except OSError as e:
            entry["consecutive_failures"] += 1
            entry["last_error"] = "internal error"
            log.error("%s: could not write payload (%s)", key, e)
            results[key] = entry
            continue

        if outcome["ref_id"] != entry.get("ref_id"):
            entry["ref_id_changed_at"] = moment.isoformat()
        entry["ref_id"] = outcome["ref_id"]
        entry["consecutive_failures"] = 0
        entry["last_error"] = None
        entry["last_success"] = moment.isoformat()
        log.info("%s ok (%s bytes)", key, outcome["bytes"])
        results[key] = entry

    if attempted == 0:
        log.warning("every endpoint was skipped by the rate guard")
        skipped = dict(previous) if previous else {"endpoints": results}
        skipped["skipped"] = True
        return skipped

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

    Counted from consecutive_failures, not last_success: last_success is
    carried forward from earlier cycles and would report success after a
    total outage.
    """
    endpoints = status.get("endpoints", {})
    return sum(1 for e in endpoints.values()
               if isinstance(e, dict) and e.get("consecutive_failures") == 0)
