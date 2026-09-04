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


class FakeResponse:
    """The two attributes core reads off a requests response."""

    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code
