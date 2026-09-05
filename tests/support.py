"""Test data and fakes shared by the test modules and by conftest.

Separate from conftest.py on purpose. pytest imports conftest.py itself as
the top-level module `conftest`, so a test module doing
`from tests.conftest import ...` imports the same file a second time under a
second name, and the two copies are distinct module objects with distinct
classes. Harmless while the shared names are constants, and a genuinely
confusing afternoon the day one of them holds mutable state or an isinstance
check. Everything importable lives here instead, and conftest.py keeps only
the fixtures.
"""

import time

# Minimal payloads that pass each endpoint's shape gate, keyed by path.
# Hand-written, not captured: no MISO data belongs in the repo.
GOOD_BODIES = {
    "/api/FuelMix": (
        b'{"RefId": "ref-fuelmix", "TotalMW": "1000", '
        b'"Fuel": {"Type": []}}'
    ),
    "/api/RealTimeTotalLoad": (
        b'{"LoadInfo": {"RefId": "ref-load", "LoadValues": []}}'
    ),
    "/api/Snapshot": (
        b'[{"t": "Current Demand (MW)", "v": "1,000", '
        b'"d": "1/01/1970 12:00:00 AM EST", "id": "demand"}]'
    ),
    "/api/WindSolar/GetCombined": (
        b'{"instance": "stub", "RefId": "ref-windsolar", '
        b'"MktDay": "1970-01-01"}'
    ),
}

# The ref_id each GOOD_BODIES payload yields, keyed by endpoint. Snapshot
# carries no RefId at any level and is permanently None.
GOOD_REF_IDS = {
    "FuelMix": "ref-fuelmix",
    "RealTimeTotalLoad": "ref-load",
    "Snapshot": None,
    "WindSolar": "ref-windsolar",
}


class FakeRaw:
    """The urllib3 object core reads the body off, one read1 at a time.

    read1 returns whatever has arrived rather than blocking for a full
    chunk - that is why core uses it - so this hands back one queued piece
    per call. `pieces` is how a test makes a body arrive gradually, and
    `delay` is how it makes it arrive slowly enough to pass a deadline.
    """

    def __init__(self, content=b"", pieces=1, delay=0.0):
        size = max(1, -(-len(content) // max(1, pieces)))
        self._queue = [content[i:i + size]
                       for i in range(0, len(content), size)] or [b""]
        self._delay = delay
        self.reads = 0

    def read1(self, n=None):
        """One piece, or the first n bytes of it with the rest pushed back.

        Returning less than asked for and keeping the remainder is what the
        real read1 does. Truncating instead would silently drop the body,
        and a size-cap test would then pass by never reaching the cap.
        """
        self.reads += 1
        if self._delay:
            time.sleep(self._delay)
        if not self._queue:
            return b""
        piece = self._queue.pop(0)
        if n is not None and len(piece) > n:
            self._queue.insert(0, piece[n:])
            piece = piece[:n]
        return piece


class FakeResponse:
    """What core reads off a requests response.

    core streams the body rather than taking response.content, so this has
    to behave like a streamed response: a context manager, with headers and
    a `raw` that yields the body a piece at a time.
    """

    def __init__(self, content=b"", status_code=200, url="http://127.0.0.1/x",
                 headers=None, pieces=1, delay=0.0):
        self.content = content
        self.status_code = status_code
        self.url = url
        self.headers = dict(headers or {})
        self.raw = FakeRaw(content, pieces=pieces, delay=delay)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False
