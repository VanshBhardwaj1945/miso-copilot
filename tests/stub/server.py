"""A local stand-in for the MISO APIs, used by the acceptance criteria.

Serves the polled paths from the synthetic fixtures beside this file, counts
requests per link, and can be told to misbehave in the specific ways criteria
6 to 14 need. It binds to 127.0.0.1 and never makes an outbound request of any
kind, so nothing here can touch MISO.

Two APIs live here, because the poller fetches from two: the four legacy
display feeds on the public API, and the paged, key-authenticated Data
Exchange fuel-type link. The Data Exchange link is served the way MISO serves
it - 401 without a subscription key, one page at a time with `page.lastPage`.

Run it from the command line:

    python -m tests.stub.server --port 8971
    python -m tests.stub.server --port 8971 --mode empty-load,empty-snapshot
    python -m tests.stub.server --port 8971 --mode de-paged --de-pages 3

or run it inside a test:

    httpd, thread = serve_in_thread(modes=["redirect"])
    base = f"http://127.0.0.1:{httpd.server_port}"
    ...
    httpd.shutdown()
    httpd.server_close()

Control endpoints, none of which are counted as link requests:

    GET /_counts                     per-link counts and current mode, JSON
    GET /_mode?mode=html             switch modes at runtime
    GET /_mode?mode=fail-one&endpoint=FuelMix
    GET /_reset                      zero the counters
    GET /_health                     liveness, JSON

Standard library only. Nothing under backend/ imports this.
"""

import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# The Data Exchange fuel-type link. Its path carries the market date, which the
# poller fills in per cycle, so it is held here in the templated form core.py
# declares and matched with the regex below. One counter for every date: the
# counters are per link, and a link that answers for a different day each
# midnight is still one link.
DE_PATH = "/lgi/v1/real-time/{date}/generation/fuel-type"
DE_PATH_RE = re.compile(
    r"^/lgi/v1/real-time/\d{4}-\d{2}-\d{2}/generation/fuel-type$")
DE_KEY_HEADER = "Ocp-Apim-Subscription-Key"

# The polled links, in the order backend/poller/core.py lists them. Keys are
# the request paths, because the counters are per link and the path is what a
# link is.
PATHS = {
    "/api/FuelMix": "FuelMix",
    "/api/RealTimeTotalLoad": "RealTimeTotalLoad",
    "/api/Snapshot": "Snapshot",
    "/api/WindSolar/GetCombined": "WindSolar",
    DE_PATH: "DEFuelMix",
}

MODES = (
    "ok",              # valid fixtures, RefId advances each time it is served
    "frozen",          # valid fixtures, RefId never changes (criterion 14)
    "html",            # 200 with an HTML body (criterion 6)
    "redirect",        # 302 with a valid Location (criterion 8)
    "empty-load",      # {"LoadInfo": {}} for RealTimeTotalLoad (criterion 7)
    "empty-snapshot",  # [{}] for Snapshot (criterion 7)
    "fail-one",        # 503 on one named link, three served (criterion 9)
    "fail-all",        # 503 on everything (criterion 13)
    "de-paged",        # Data Exchange answers in --de-pages pages, not one
    "de-401",          # Data Exchange rejects the key it is given
)


def link_for(path: str):
    """The counter key for a request path, or None if nothing serves it.

    Every date the Data Exchange link is asked for resolves to the one
    templated path, so a cycle that straddles EST midnight does not look like
    two links that were each hit once.
    """
    if path in PATHS:
        return path
    if DE_PATH_RE.match(path):
        return DE_PATH
    return None


HTML_BODY = (
    b"<!doctype html>\n<html><head><title>Stub error page</title></head>\n"
    b"<body><h1>503 Service Unavailable</h1>\n"
    b"<p>This is the stub pretending to be an HTML error page.</p>\n"
    b"</body></html>\n"
)

# Where the RefId sits in each fixture, mirroring core.py's ref_path column.
# Snapshot is absent on purpose: it carries no RefId at any level.
REF_PATHS = {
    "FuelMix": ("RefId",),
    "RealTimeTotalLoad": ("LoadInfo", "RefId"),
    "WindSolar": ("RefId",),
}


def parse_modes(text: str) -> list:
    """Split a comma separated mode string and reject anything unknown.

    Modes combine because criterion 7 needs two of them at once: a stub
    returning {"LoadInfo": {}} for RealTimeTotalLoad *and* [{}] for Snapshot
    in the same run.
    """
    modes = [part.strip() for part in text.split(",") if part.strip()]
    if not modes:
        modes = ["ok"]
    for mode in modes:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}, expected one of "
                             + ", ".join(MODES))
    return modes


def load_fixtures() -> dict:
    """Read every synthetic payload off disk, parsed.

    Loaded once at startup so a run cannot half-see an edited fixture, and
    parsed rather than kept as bytes because ok mode rewrites the RefId.
    """
    fixtures = {}
    for key in PATHS.values():
        path = FIXTURE_DIR / f"{key}.json"
        fixtures[key] = json.loads(path.read_text())
    return fixtures


class StubState:
    """Everything the handler needs, shared across threads behind one lock."""

    def __init__(self, modes=None, fail_endpoint="FuelMix",
                 rotate_ref_id=True, de_pages=3):
        self.lock = threading.Lock()
        self.modes = list(modes or ["ok"])
        self.fail_endpoint = fail_endpoint
        self.rotate_ref_id = rotate_ref_id
        self.de_pages = max(1, de_pages)
        self.counts = {path: 0 for path in PATHS}
        self.revisions = {path: 0 for path in PATHS}
        self.de_keys = []
        self.fixtures = load_fixtures()

    def has(self, mode: str) -> bool:
        return mode in self.modes

    def set_modes(self, modes, fail_endpoint=None) -> None:
        with self.lock:
            self.modes = list(modes)
            if fail_endpoint is not None:
                self.fail_endpoint = fail_endpoint

    def reset_counts(self) -> None:
        with self.lock:
            for path in self.counts:
                self.counts[path] = 0
                self.revisions[path] = 0
            self.de_keys.clear()

    def record_key(self, key) -> None:
        """Remember the subscription key one Data Exchange request carried.

        How a test proves the key reached the wire as a header rather than
        being dropped or smuggled into the query string. Only ever a synthetic
        test key: nothing but the suite can reach this server.
        """
        with self.lock:
            self.de_keys.append(key)

    def count(self, path: str) -> int:
        """Record one request against a link and return its new revision."""
        with self.lock:
            self.counts[path] += 1
            self.revisions[path] += 1
            return self.revisions[path]

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "counts": dict(self.counts),
                "total": sum(self.counts.values()),
                "modes": list(self.modes),
                "fail_endpoint": self.fail_endpoint,
                "rotate_ref_id": self.rotate_ref_id,
                "de_pages": self.de_pages,
                "de_keys": list(self.de_keys),
            }


def _set_ref_id(body, key: str, value: str) -> None:
    """Overwrite the RefId of a parsed fixture in place. Snapshot has none."""
    ref_path = REF_PATHS.get(key)
    if ref_path is None:
        return
    target = body
    for step in ref_path[:-1]:
        target = target[step]
    target[ref_path[-1]] = value


def payload_for(state: StubState, key: str, revision: int) -> bytes:
    """The valid body for one link, with the RefId set for this request.

    In ok mode the RefId advances every time a link is served, so a poller
    sees a live feed and ref_id_changed_at moves. In frozen mode it stays at
    the fixture value, which is what criterion 14 watches: last_success
    advances while ref_id_changed_at does not.
    """
    body = json.loads(json.dumps(state.fixtures[key]))
    if state.rotate_ref_id and not state.has("frozen"):
        base = "01-Jan-1970 - Interval 00:00 EST"
        _set_ref_id(body, key, f"{base} rev {revision}")
    return json.dumps(body, indent=2).encode()


def _int_param(query: dict, name: str, default: int) -> int:
    """One integer query parameter, falling back to `default` if absent or junk."""
    try:
        return int(query.get(name, [default])[0])
    except (TypeError, ValueError):
        return default


def de_payload(state: StubState, query: dict) -> bytes:
    """One page of the Data Exchange fuel-type link, cut from the fixture.

    In de-paged mode the fixture's rows are dealt out over `de_pages` pages
    and `lastPage` arrives only on the last one, which is what drives the
    poller's multi-page assembly. Otherwise the whole fixture is page one.
    """
    body = json.loads(json.dumps(state.fixtures["DEFuelMix"]))
    rows = body["data"]
    pages = state.de_pages if state.has("de-paged") else 1
    number = _int_param(query, "pageNumber", 1)
    per_page = -(-len(rows) // pages)      # ceiling, so `pages` pages hold them all
    start = (number - 1) * per_page
    body["data"] = rows[start:start + per_page]
    body["page"] = {
        "pageNumber": number,
        "pageSize": _int_param(query, "pageSize", len(rows)),
        "lastPage": number >= pages,
        "totalCount": len(rows),
    }
    return json.dumps(body, indent=2).encode()


def de_response(state: StubState, subscription_key, query: dict):
    """The Data Exchange link: the subscription key first, then one page.

    An unkeyed request gets MISO's own 401 rather than a payload, because that
    is what the real API does - so a test asserting the header reached the wire
    is asserting something, not watching a stub that would have served anyone.
    de-401 mode is a key the API rejects: same status, different reason.
    """
    json_headers = [("Content-Type", "application/json")]
    if state.has("de-401") or not subscription_key:
        reason = ("Access denied due to invalid subscription key"
                  if subscription_key
                  else "Access denied due to missing subscription key")
        body = json.dumps({"statusCode": 401, "message": reason}).encode()
        return 401, json_headers, body
    return 200, json_headers, de_payload(state, query)


def response_for(state: StubState, path: str, revision: int, port: int,
                 query=None, subscription_key=None):
    """Decide status, headers and body for one link request.

    Returns (status, headers, body). Precedence runs from the broadest
    failure to the narrowest, so fail-all beats fail-one beats a bad body,
    and the per-link behaviors - the empty payloads, the Data Exchange key
    check - sit at the narrow end.
    """
    key = PATHS[link_for(path)]
    json_headers = [("Content-Type", "application/json")]

    if state.has("fail-all"):
        return 503, [("Content-Type", "text/plain")], b"stub: fail-all\n"

    if state.has("fail-one") and key == state.fail_endpoint:
        return 503, [("Content-Type", "text/plain")], b"stub: fail-one\n"

    if state.has("html"):
        return 200, [("Content-Type", "text/html")], HTML_BODY

    if state.has("redirect"):
        location = f"http://127.0.0.1:{port}{path}?moved=1"
        headers = [("Content-Type", "text/plain"), ("Location", location)]
        return 302, headers, b"stub: moved\n"

    if key == "DEFuelMix":
        return de_response(state, subscription_key, query or {})

    if state.has("empty-load") and key == "RealTimeTotalLoad":
        return 200, json_headers, json.dumps({"LoadInfo": {}}).encode()

    if state.has("empty-snapshot") and key == "Snapshot":
        return 200, json_headers, json.dumps([{}]).encode()

    return 200, json_headers, payload_for(state, key, revision)


class StubHandler(BaseHTTPRequestHandler):
    """One request. Control endpoints first, then the four links, then 404."""

    server_version = "MisoStub/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        """Quiet by default so a threaded test run is readable."""
        if getattr(self.server, "verbose", False):
            sys.stderr.write("stub: " + (fmt % args) + "\n")

    def _send(self, status, headers, body: bytes) -> None:
        """Write one response. Content-Length is always set, so HTTP/1.1
        keep-alive does not hang a client waiting for more bytes."""
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, obj) -> None:
        body = json.dumps(obj, indent=2).encode() + b"\n"
        self._send(status, [("Content-Type", "application/json")], body)

    def do_GET(self):
        state = self.server.state
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)

        if path == "/_counts":
            self._send_json(200, state.snapshot())
            return

        if path == "/_health":
            self._send_json(200, {"ok": True, "modes": state.modes})
            return

        if path == "/_reset":
            state.reset_counts()
            self._send_json(200, state.snapshot())
            return

        if path == "/_mode":
            wanted = query.get("mode", ["ok"])[0]
            endpoint = query.get("endpoint", [None])[0]
            try:
                modes = parse_modes(wanted)
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
                return
            if endpoint is not None and endpoint not in PATHS.values():
                self._send_json(
                    400,
                    {"ok": False, "error": f"unknown endpoint {endpoint!r}"})
                return
            state.set_modes(modes, endpoint)
            self._send_json(200, state.snapshot())
            return

        link = link_for(path)
        if link is None:
            self._send_json(
                404, {"ok": False, "error": f"no such path {path!r}"})
            return

        # Counted before the response is decided, so a 302 or a 503 counts
        # exactly like a served payload. Criterion 8 depends on that: the
        # count proves the redirect was seen once and not followed. A paged
        # fetch counts once per page for the same reason - the count is what
        # says how hard one link was hit.
        revision = state.count(link)
        subscription_key = self.headers.get(DE_KEY_HEADER)
        if link == DE_PATH:
            state.record_key(subscription_key)
        status, headers, body = response_for(
            state, path, revision, self.server.server_port,
            query=query, subscription_key=subscription_key)
        self._send(status, headers, body)


class StubServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that can be shut down before it ever served.

    BaseServer.shutdown() blocks on a flag that only serve_forever()'s
    finally clause ever sets, so shutting down a server that was built and
    never served waits forever. Building is a separate step from serving
    here, and a caller that gives up in between - a fixture that fails after
    make_server(), a test that decides it does not need the stub after all -
    must still be able to stop it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.serving = threading.Event()

    def serve_forever(self, *args, **kwargs):
        self.serving.set()
        try:
            super().serve_forever(*args, **kwargs)
        finally:
            self.serving.clear()

    def shutdown(self) -> None:
        """Stop serve_forever(). Does nothing if it is not running."""
        if self.serving.is_set():
            super().shutdown()


def make_server(port: int = 0, modes=None, fail_endpoint: str = "FuelMix",
                rotate_ref_id: bool = True, de_pages: int = 3,
                verbose: bool = False):
    """Build a stub bound to 127.0.0.1. Port 0 asks the OS for a free one.

    The caller runs it: serve_forever() for a script, serve_in_thread() for
    a test. Read the chosen port off httpd.server_port. Stop it with
    httpd.shutdown() then httpd.server_close(), which is safe whether or not
    it ever started serving.
    """
    httpd = StubServer(("127.0.0.1", port), StubHandler)
    httpd.daemon_threads = True
    httpd.state = StubState(modes=modes, fail_endpoint=fail_endpoint,
                            rotate_ref_id=rotate_ref_id, de_pages=de_pages)
    httpd.verbose = verbose
    return httpd


def serve_in_thread(port: int = 0, modes=None, fail_endpoint: str = "FuelMix",
                    rotate_ref_id: bool = True, de_pages: int = 3,
                    verbose: bool = False):
    """Start a stub on a daemon thread. Returns (httpd, thread).

    Stop it with httpd.shutdown() then httpd.server_close(). Returns only
    once the thread is actually serving, so a caller that stops it on the
    next line stops a server that started.
    """
    httpd = make_server(port=port, modes=modes, fail_endpoint=fail_endpoint,
                        rotate_ref_id=rotate_ref_id, de_pages=de_pages,
                        verbose=verbose)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                              name="miso-stub")
    thread.start()
    httpd.serving.wait(5)
    return httpd, thread


def main(argv=None) -> int:
    """Command line entry point. Prints the port, then serves until Ctrl-C."""
    parser = argparse.ArgumentParser(
        prog="tests.stub.server",
        description="Local stand-in for the MISO public and Data Exchange APIs.")
    parser.add_argument("--port", type=int, default=8971,
                        help="port to bind on 127.0.0.1, 0 for any free port")
    parser.add_argument("--mode", default="ok",
                        help="one mode or a comma separated list: "
                             + ", ".join(MODES))
    parser.add_argument("--fail-endpoint", default="FuelMix",
                        choices=sorted(PATHS.values()),
                        help="which link fail-one mode fails")
    parser.add_argument("--no-rotate", action="store_true",
                        help="serve the fixture RefId verbatim in ok mode")
    parser.add_argument("--de-pages", type=int, default=3,
                        help="how many pages de-paged mode splits the Data "
                             "Exchange fixture across")
    parser.add_argument("--verbose", action="store_true",
                        help="log every request to stderr")
    args = parser.parse_args(argv)

    try:
        modes = parse_modes(args.mode)
    except ValueError as e:
        parser.error(str(e))

    httpd = make_server(port=args.port, modes=modes,
                        fail_endpoint=args.fail_endpoint,
                        rotate_ref_id=not args.no_rotate,
                        de_pages=args.de_pages,
                        verbose=args.verbose)
    base = f"http://127.0.0.1:{httpd.server_port}"
    print(f"stub listening on {base}")
    print(f"stub port {httpd.server_port}")
    print(f"stub modes {','.join(modes)}")
    print(f"stub counts at {base}/_counts")
    sys.stdout.flush()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stub stopping")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
