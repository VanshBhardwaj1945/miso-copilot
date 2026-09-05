"""Fetch four MISO public JSON endpoints and write them verbatim to data/raw/.

The bytes MISO returns are the bytes written to disk; the RAG lane reads them
and does everything downstream. Two exceptions to "verbatim": a shape gate that
rejects HTML error pages, and a `ref_id` lifted out so a frozen feed shows in
the status file. Each endpoint's `outcome` (ok / failed / skipped) is the one
field that describes THIS cycle; everything else carries forward.

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
import zlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
import urllib3

from backend.poller import guard

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://public-api.misoenergy.org"
TIMEZONE = ZoneInfo("America/Indiana/Indianapolis")
TIMEOUT = (5, 15)
# requests' read timeout is per socket read; a server dripping bytes never trips
# it, so the body read carries its own total deadline
MAX_REQUEST_SECONDS = 20
# real payloads are ~12 KB; this process also serves /ask, so bodies are capped
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
BODY_CHUNK_BYTES = 64 * 1024
HEADERS = {
    "User-Agent": "miso-copilot/0.1 (MISO Xtern Challenge 2026)",
    "Accept": "application/json",
    # the body is read raw (see _read_body) and decoded here, so only ask for
    # encodings this file can decode
    "Accept-Encoding": "gzip, deflate",
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
    """Non-empty array whose every row has MISO's four fields (Snapshot has no RefId to gate on)."""
    return (
        isinstance(body, list)
        and len(body) > 0
        and all(isinstance(row, dict) and {"t", "v", "d", "id"} <= set(row)
                for row in body)
    )


def _shape_windsolar(body: object) -> bool:
    return isinstance(body, dict) and {"instance", "RefId", "MktDay"} <= set(body)


class Endpoint(NamedTuple):
    """One polled link: where it lives, what it must look like, where its RefId is."""

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

def safe_base(base: str) -> str:
    """`base` with any user:password stripped - it reaches logs and the status file."""
    parts = urlsplit(base)
    if not parts.hostname:
        return base
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", "")).rstrip("/")


def base_url() -> str:
    """MISO's base URL (or a stub), normalized: the guard key and the URL sent must be the same string."""
    raw = (os.environ.get("MISO_API_BASE") or DEFAULT_BASE_URL).strip()
    parts = urlsplit(raw)
    if parts.scheme and parts.netloc:
        raw = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return raw.rstrip("/")


def raw_dir() -> Path:
    """Where payloads are written: MISO_RAW_DIR if set, else <repo>/data/raw."""
    override = os.environ.get("MISO_RAW_DIR")
    if override:
        # resolved, because the status file records the absolute dir written
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
    """Whether FastAPI should register the scheduled job; every spelling of "off" counts."""
    raw = os.environ.get("MISO_POLLER_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def base_is_loopback(base: str) -> bool:
    """True only when `base` provably names this machine - the rate-guard bypass.

    Keyed on the destination, not the spelling: comparing strings to the
    default URL once let `http://`, a capital letter or `:443` switch the guard
    off while still hitting MISO. Loopback bypasses; anything else is guarded.
    No DNS lookup here - this runs in front of every fetch.
    """
    host = urlsplit(base).hostname
    if host is None:
        return False
    host = host.rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def now() -> datetime:
    """Our wall clock, offset-aware (local time; MISO's own stamps are fixed EST)."""
    return datetime.now(TIMEZONE)


# --- atomic writes ----------------------------------------------------------

def write_atomic(path: Path, payload: bytes) -> None:
    """Write bytes so a reader sees the old file or the new one, never a partial.

    Unique temp name (two writers on one ".tmp" can truncate each other), then
    an atomic os.replace. The ".tmp" suffix is what sweep_stale_tmp globs for.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        # fdopen owns fd only once it returns; close it ourselves if it raises
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)   # mkstemp makes 0600; the RAG lane reads these
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)


def sweep_stale_tmp(directory: Path) -> None:
    """Delete leftover .tmp files older than STALE_TMP_SECONDS (never a live writer's)."""
    cutoff = time.time() - STALE_TMP_SECONDS
    for leftover in directory.glob("*.tmp"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
                log.info("swept stale temp file %s", leftover.name)
        except OSError:
            pass


# --- status file ------------------------------------------------------------

# the keys carried forward between cycles; `outcome` is deliberately absent
# because it describes one cycle and must never be inherited
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
    """Load _status.json; missing, unparseable or mis-shaped all mean "start fresh"."""
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
    """Whether a stored entry is safe to build on - the file is hand-editable, so type-check it."""
    if not isinstance(entry, dict):
        return False
    failures = entry.get("consecutive_failures")
    # bool is an int subclass; `true` must not count up from 1
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
    """Parse and gate one 200 body: JSON, shape, ref_id. Pure - no socket needed to test."""
    try:
        body = json.loads(content)
    except (ValueError, RecursionError):   # absurdly nested JSON raises the latter
        return _fetch_failure("invalid JSON", status, size)

    if not endpoint.shape(body):
        return _fetch_failure("shape check failed", status, size)

    ref_id = extract_ref_id(body, endpoint.ref_path)
    if endpoint.ref_path is not None and ref_id is None:
        return _fetch_failure("missing ref_id", status, size)

    return {"ok": True, "error": None, "http_status": status, "bytes": size,
            "ref_id": ref_id, "content": content}


# _read_body reads response.raw directly, so urllib3's exceptions arrive
# unwrapped alongside requests' own
TRANSPORT_ERRORS = (requests.exceptions.RequestException,
                    urllib3.exceptions.HTTPError,
                    OSError)


def _transport_error(e: Exception, endpoint: Endpoint) -> str:
    """The closed-vocabulary name for a transport exception. Order matters: subclasses first."""
    if isinstance(e, (requests.exceptions.MissingSchema,
                      requests.exceptions.InvalidURL)):
        return "bad base url"
    if isinstance(e, (requests.exceptions.Timeout,
                      urllib3.exceptions.TimeoutError)):
        return "timeout"
    if isinstance(e, (requests.exceptions.ChunkedEncodingError,
                      requests.exceptions.ContentDecodingError,
                      urllib3.exceptions.ProtocolError,
                      urllib3.exceptions.DecodeError,
                      urllib3.exceptions.IncompleteRead,
                      urllib3.exceptions.InvalidChunkLength)):
        return "truncated response"
    if isinstance(e, (requests.exceptions.ConnectionError,
                      ConnectionError)):
        return "connection error"
    log.warning("unexpected request failure for %s", endpoint.key,
                exc_info=True)
    return "internal error"


def _decoder_for(response):
    """A decompressor for this reply, None if plain, or "unsupported"."""
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
    if encoding in ("", "identity"):
        return None
    if encoding == "gzip":
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        return zlib.decompressobj()
    return "unsupported"


def _read_body(response, deadline: float) -> tuple[bytes, str | None]:
    """The body, or (b"", reason) if it is too large, too slow, or unreadable.

    raw.read1 is the one read that returns whatever has arrived: response.content
    has no size cap and iter_content blocks until a full chunk exists, so
    neither lets a deadline be enforced. Compressed and decompressed sizes are
    capped separately - a few compressed KB can expand into gigabytes.
    """
    decoder = _decoder_for(response)
    if decoder == "unsupported":
        log.warning("%s: cannot decode Content-Encoding %r",
                    response.url, response.headers.get("Content-Encoding"))
        return b"", "unexpected encoding"

    chunks, received, produced = [], 0, 0
    while True:
        chunk = response.raw.read1(BODY_CHUNK_BYTES)
        if not chunk:
            break
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            log.warning("%s: reply passed %d bytes, refusing it",
                        response.url, MAX_RESPONSE_BYTES)
            return b"", "response too large"
        if time.monotonic() > deadline:
            log.warning("%s: reply still arriving after %ds, giving up",
                        response.url, MAX_REQUEST_SECONDS)
            return b"", "timeout"
        if decoder is not None:
            try:
                # max_length stops a compression bomb before it is allocated
                chunk = decoder.decompress(
                    chunk, MAX_RESPONSE_BYTES - produced + 1)
            except zlib.error:
                log.warning("%s: compressed body did not decode",
                            response.url)
                return b"", "truncated response"
        produced += len(chunk)
        if produced > MAX_RESPONSE_BYTES:
            log.warning("%s: body reached %d bytes, refusing it",
                        response.url, MAX_RESPONSE_BYTES)
            return b"", "response too large"
        chunks.append(chunk)

    if decoder is not None:
        try:
            chunks.append(decoder.flush())
        except zlib.error:
            return b"", "truncated response"
        if not decoder.eof:
            # zlib does not raise on a stream that ends early; catch it here
            log.warning("%s: compressed body ended early", response.url)
            return b"", "truncated response"
    return b"".join(chunks), None


def _fetch(endpoint: Endpoint, url: str) -> dict:
    """One request. Never raises for a MISO-side problem; every path returns the same six keys.

    Errors are a closed vocabulary, never raw exception text - that string
    reaches a file the RAG lane may render.
    """
    deadline = time.monotonic() + MAX_REQUEST_SECONDS
    try:
        response = requests.get(url, timeout=TIMEOUT, headers=HEADERS,
                                allow_redirects=False, stream=True)
    except TRANSPORT_ERRORS as e:
        return _fetch_failure(_transport_error(e, endpoint))

    # with stream=True the transport can still fail while reading the body
    try:
        with response:
            status = response.status_code
            content, refused = _read_body(response, deadline)
    except TRANSPORT_ERRORS as e:
        return _fetch_failure(_transport_error(e, endpoint))

    if refused is not None:
        return _fetch_failure(refused, status)

    size = len(content)
    if 300 <= status < 400:
        # requests drains a redirect body itself, so count what it already read
        return _fetch_failure(f"redirect {status}", status,
                              size or len(response.content))
    if status != 200:
        return _fetch_failure(f"HTTP {status}", status, size)

    return _validate(endpoint, content, status, size)


# --- one endpoint, end to end -----------------------------------------------

def _skipped_observation() -> dict:
    """What a rate-guard skip leaves behind: nothing was attempted, so the stored error survives."""
    return {"outcome": "skipped", "last_attempt": None, "http_status": None,
            "bytes": None, "last_error": None, "ref_id": None}


def _internal_error_observation() -> dict:
    """What an endpoint that raised out of _poll_endpoint leaves behind."""
    return {"outcome": "failed", "last_attempt": now().isoformat(),
            "http_status": None, "bytes": None,
            "last_error": "internal error", "ref_id": None}


def _poll_endpoint(endpoint: Endpoint, url: str, directory: Path,
                   bypass_guard: bool) -> dict:
    """Claim, fetch, validate and write one endpoint. Returns this cycle's observation.

    Never reads the stored status: the observation is merged onto it later,
    under the status lock, so no network request happens inside that lock.
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
    """Fold one cycle's observation onto the stored entry (read under the lock). Mutates `entry`."""
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

# the status file is hand-editable, so a key is not a safe filename until proven
SAFE_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _prune_payloads(directory: Path, previous_endpoints: dict,
                    current: dict) -> None:
    """Delete the payload of any endpoint that left the table - exact filename only, never a glob."""
    # casefolded: macOS and Windows filesystems are case-insensitive, so
    # "Windsolar" and "WindSolar" (or "_STATUS") name the same file on disk
    status_key = status_path(directory).stem.casefold()
    live = {k.casefold() for k in current}
    for key in previous_endpoints:
        if isinstance(key, str) and key.casefold() in live:
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
    """Announce a non-default base or raw dir every cycle, saying whether the guard is on."""
    if base != DEFAULT_BASE_URL:
        if bypass_guard:
            log.warning("MISO_API_BASE is %s - loopback, RATE GUARD BYPASSED",
                        safe_base(base))
        else:
            log.warning("MISO_API_BASE is %s - not loopback, rate guard ACTIVE",
                        safe_base(base))
    if os.environ.get("MISO_RAW_DIR"):
        log.warning("MISO_RAW_DIR is set, writing to %s", directory)


def _all_skipped_status(directory: Path) -> dict:
    """The stored status (or a synthesized one) when the guard skipped every endpoint. Nothing is written."""
    previous = read_status(directory)
    unchanged = dict(previous) if previous else {
        "endpoints": {endpoint.key: dict(INITIAL_ENTRY, path=endpoint.path,
                                         outcome="skipped")
                      for endpoint in ENDPOINTS}}
    unchanged["skipped"] = True
    return unchanged


def _write_status(directory: Path, observations: dict, base: str,
                  started: datetime) -> dict:
    """Merge this cycle onto the stored status and write it, as one locked read-merge-write."""
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
            "base_url": safe_base(base),
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

    # all network requests happen here, with no lock held and the status file
    # not yet read (reading it first is how two cycles once merged onto stale entries)
    observations = {}
    for endpoint in ENDPOINTS:
        key = endpoint.key
        try:
            observations[key] = _poll_endpoint(
                endpoint, base + endpoint.path, directory, bypass_guard)
        except Exception:
            # _fetch names every failure it can foresee; this is a bug in our code
            log.exception("%s: unexpected failure", key)
            observations[key] = _internal_error_observation()

    if all(o["outcome"] == "skipped" for o in observations.values()):
        log.warning("every endpoint was skipped by the rate guard")
        return _all_skipped_status(directory)

    return _write_status(directory, observations, base, started)


def succeeded_count(status: dict) -> int:
    """How many endpoints succeeded this cycle - read from `outcome`, the only per-cycle field."""
    endpoints = status.get("endpoints", {})
    return sum(1 for e in endpoints.values()
               if isinstance(e, dict) and e.get("outcome") == "ok")
