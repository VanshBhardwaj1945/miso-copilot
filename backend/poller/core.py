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
# requests' read timeout is per socket read, not a total. A server that sends
# one byte every few seconds keeps every individual read comfortably inside
# TIMEOUT[1] and the request never returns - measured, it was still running
# after 90 seconds. With max_instances=1 on the scheduler one such reply stops
# polling permanently and silently, so the body read carries its own deadline.
MAX_REQUEST_SECONDS = 20
# response.content buffers the whole body with no cap. A 256 MB reply cost
# 582 MB of resident memory when measured, against a 28 MB baseline, and the
# poller shares a process with the API serving /ask. The largest real payload
# is about 12 KB, so this is roughly 2700x headroom and still bounded.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
BODY_CHUNK_BYTES = 64 * 1024
HEADERS = {
    "User-Agent": "miso-copilot/0.1 (MISO Xtern Challenge 2026)",
    "Accept": "application/json",
    # Spelled out rather than left to requests, because the body is read with
    # raw.read1 - the one primitive that returns what has arrived instead of
    # blocking until a full chunk exists - and read1 hands back bytes exactly
    # as they came off the socket, undecoded. This lane therefore decodes
    # them itself, and may only ask for encodings it can decode. MISO does
    # serve these gzipped (spec 5.1), so refusing compression outright would
    # mean refusing MISO.
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

def safe_base(base: str) -> str:
    """`base` with any userinfo removed, for logging and for the status file.

    A base of the form http://user:pw@host would otherwise reach a WARNING
    line every cycle, the `base_url` field in _status.json, and the --status
    screen. None of those should ever carry a password.
    """
    parts = urlsplit(base)
    if not parts.hostname:
        return base
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", "")).rstrip("/")


def base_url() -> str:
    """MISO's base URL, or a stub, normalized so it can be joined and compared.

    Query and fragment are dropped, not just trailing slashes. requests
    strips a fragment before sending, so a base carrying one would hand the
    guard four distinct keys while putting four identical requests on the
    wire - the guard satisfied, one link hit four times a cycle. The key we
    claim and the URL we send have to be the same string.
    """
    raw = (os.environ.get("MISO_API_BASE") or DEFAULT_BASE_URL).strip()
    parts = urlsplit(raw)
    if parts.scheme and parts.netloc:
        raw = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return raw.rstrip("/")


def raw_dir() -> Path:
    """Where payloads are written.

    Anchored to the repo root derived from this file, never to the working
    directory, so `python -m backend.poller` writes the same place from
    anywhere. This file is backend/poller/core.py, so the repo root is three
    parents up - one more than backend/config.py needs.
    """
    override = os.environ.get("MISO_RAW_DIR")
    if override:
        # Resolved, because the status file records `raw_dir` as the
        # absolute directory actually written. A relative override
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
    # A non-negative integer is required, and true is not one.
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


# requests wraps urllib3's exceptions, but only on the paths that go through
# requests. _read_body calls response.raw.read1 directly - the one primitive
# that makes a deadline enforceable - and urllib3's own exceptions come back
# out of it unwrapped. Both families have to be named here or a plain read
# timeout arrives as "internal error" with a traceback, which is a lie about
# whose fault it is.
TRANSPORT_ERRORS = (requests.exceptions.RequestException,
                    urllib3.exceptions.HTTPError,
                    OSError)


def _transport_error(e: Exception, endpoint: Endpoint) -> str:
    """The closed-vocabulary name for a transport exception.

    Order is load-bearing twice over. requests' ConnectTimeout subclasses
    ConnectionError before Timeout, so a connect timeout reads as a
    connection error unless Timeout is tested first. urllib3's IncompleteRead
    subclasses HTTPError before http.client.IncompleteRead, so a truncated
    body needs naming before the generic case.
    """
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
    """A decompressor for this reply, None if it is plain, or "unsupported".

    zlib handles both encodings requests is allowed to ask for above: gzip
    needs the 16 + MAX_WBITS window that tells zlib to expect a gzip header,
    deflate is the bare stream.
    """
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

    Read with raw.read1 rather than iter_content or response.content, and the
    choice is the whole point of this function.

    response.content buffers the entire reply with no cap: a 256 MB reply cost
    582 MB of resident memory when measured, and this process also serves
    /ask. iter_content caps memory but not time - it blocks until it has a
    full chunk, so a server dripping one byte at a time never yields control
    and the deadline below would never be tested. read1 is the one primitive
    that returns whatever has arrived, which is what makes a deadline
    enforceable at all.

    Both the compressed and the decompressed size are capped, because they
    are two different ways to be hurt. Capping only what arrives would let a
    few compressed KB expand into gigabytes; capping only the result would
    let a slow enormous reply occupy the socket regardless.
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
                # max_length is what stops a compression bomb: zlib returns
                # at most that many bytes and keeps the rest, so the check
                # below sees the overflow without anything having allocated
                # the full expansion first.
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
            # The compressed stream ended before its own end marker. zlib
            # does not raise for this - it just returns what it managed -
            # so without the check a half payload would go on to fail the
            # JSON gate and be reported as MISO sending bad JSON.
            log.warning("%s: compressed body ended early", response.url)
            return b"", "truncated response"
    return b"".join(chunks), None


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
    deadline = time.monotonic() + MAX_REQUEST_SECONDS
    try:
        response = requests.get(url, timeout=TIMEOUT, headers=HEADERS,
                                allow_redirects=False, stream=True)
    except TRANSPORT_ERRORS as e:
        return _fetch_failure(_transport_error(e, endpoint))

    # The body is read inside its own try: with stream=True the transport can
    # still fail here, long after the headers arrived, and those failures
    # belong in the same closed vocabulary as the ones above.
    try:
        with response:
            status = response.status_code
            content, refused = _read_body(response, deadline)
    except TRANSPORT_ERRORS as e:
        return _fetch_failure(_transport_error(e, endpoint))

    if refused is not None:
        # No size: nothing was kept, and a partial count would read as though
        # that many bytes had been accepted.
        return _fetch_failure(refused, status)

    size = len(content)
    if 300 <= status < 400:
        # requests drains a redirect's body itself, to free the connection
        # while it works out where the redirect points. That is why the
        # read above saw nothing: the bytes are already in memory. Falling
        # back to them costs no read and keeps 6.3's rule that a body which
        # arrived is always counted, redirect or not.
        return _fetch_failure(f"redirect {status}", status,
                              size or len(response.content))
    if status != 200:
        return _fetch_failure(f"HTTP {status}", status, size)

    return _validate(endpoint, content, status, size)


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

    A pruned status entry whose .json is still sitting
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
    # Casefolded, because macOS and Windows filesystems are
    # case-insensitive: a stale entry keyed "Windsolar" names the same
    # file on disk as the live "WindSolar", so a case-sensitive test
    # would delete the payload this very cycle just wrote and leave the
    # status file swearing it succeeded.
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
    """Announce a non-default base and raw directory, once per cycle.

    A non-default base has to be loud every cycle.
    Whether the guard is on is a separate sentence, because a non-default
    base that is not loopback still reaches MISO and is still guarded, and an
    operator skimming the log must be able to tell the two apart at a glance.
    """
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
            # Any exception in one endpoint is
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
