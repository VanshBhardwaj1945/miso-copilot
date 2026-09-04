"""The validation gate: _validate, the four _shape_* checks, extract_ref_id.

These are pure functions over bytes. Nothing here opens a socket or touches
the filesystem, so every case is a literal payload and an assertion on the
result dict.

The gate is what stands between an HTML error page and data/raw/. Spec
sections 5.4 and 6.4 define it; the `last_error` strings asserted here are
the closed vocabulary from section 6.3 and are matched exactly, because the
RAG lane reads them.
"""

import json

import pytest

from backend.poller import core
from tests.conftest import GOOD_BODIES, GOOD_REF_IDS


def endpoint(key):
    """The Endpoint named `key`, straight out of the live table."""
    return next(e for e in core.ENDPOINTS if e.key == key)


def validate(key, body: bytes, status: int = 200):
    """Run the gate on one payload, with `bytes` derived from the body."""
    return core._validate(endpoint(key), body, status, len(body))


ALL_KEYS = ["FuelMix", "RealTimeTotalLoad", "Snapshot", "WindSolar"]


# --- each shape accepts its own valid payload -------------------------------

@pytest.mark.parametrize("key", ALL_KEYS)
def test_valid_payload_passes_the_gate(key):
    body = GOOD_BODIES[endpoint(key).path]
    result = validate(key, body)
    assert result["ok"] is True
    assert result["error"] is None


@pytest.mark.parametrize("key", ALL_KEYS)
def test_valid_payload_returns_the_bytes_unchanged(key):
    # Section 5.1: the parsed object is never written, the body is.
    body = GOOD_BODIES[endpoint(key).path]
    assert validate(key, body)["content"] is body


@pytest.mark.parametrize("key", ALL_KEYS)
def test_valid_payload_reports_its_ref_id(key):
    body = GOOD_BODIES[endpoint(key).path]
    assert validate(key, body)["ref_id"] == GOOD_REF_IDS[key]


def test_snapshot_passes_with_no_ref_id_at_all():
    # Snapshot is exempt from the ref_id gate: it has no RefId anywhere.
    result = validate("Snapshot", GOOD_BODIES["/api/Snapshot"])
    assert result["ok"] is True
    assert result["ref_id"] is None


def test_valid_payload_records_status_and_size():
    body = GOOD_BODIES["/api/FuelMix"]
    result = validate("FuelMix", body, status=200)
    assert (result["http_status"], result["bytes"]) == (200, len(body))


# --- each shape rejects the wrong shape -------------------------------------

def test_fuelmix_rejects_a_list():
    assert validate("FuelMix", b"[]")["error"] == "shape check failed"


def test_fuelmix_rejects_an_object_missing_totalmw():
    body = b'{"RefId": "r", "Fuel": {}}'
    assert validate("FuelMix", body)["error"] == "shape check failed"


def test_load_rejects_an_object_without_loadinfo():
    body = b'{"RefId": "r"}'
    assert validate("RealTimeTotalLoad", body)["error"] == "shape check failed"


def test_load_rejects_a_list():
    body = b'[{"LoadInfo": {"RefId": "r"}}]'
    assert validate("RealTimeTotalLoad", body)["error"] == "shape check failed"


def test_snapshot_rejects_an_object():
    body = b'{"t": "x", "v": "1", "d": "d", "id": "i"}'
    assert validate("Snapshot", body)["error"] == "shape check failed"


def test_snapshot_rejects_an_empty_array():
    assert validate("Snapshot", b"[]")["error"] == "shape check failed"


def test_windsolar_rejects_an_object_missing_mktday():
    body = b'{"instance": "s", "RefId": "r"}'
    assert validate("WindSolar", body)["error"] == "shape check failed"


def test_windsolar_rejects_a_list():
    assert validate("WindSolar", b"[]")["error"] == "shape check failed"


# --- the shaped-but-empty holes, spec 5.4 -----------------------------------

def test_empty_loadinfo_is_missing_ref_id_not_a_shape_failure():
    # The case that motivated the ref_id check: valid JSON, the right key,
    # and nothing in it. The shape gate alone would have written this.
    result = validate("RealTimeTotalLoad", b'{"LoadInfo": {}}')
    assert result["error"] == "missing ref_id"


def test_empty_loadinfo_is_not_written():
    result = validate("RealTimeTotalLoad", b'{"LoadInfo": {}}')
    assert result["ok"] is False
    assert result["content"] is None


def test_snapshot_of_one_empty_object_fails_the_shape_check():
    # Snapshot has no RefId, so the per-row element check is its only
    # guard. Without it, `[{}]` would overwrite good data as a success.
    assert validate("Snapshot", b"[{}]")["error"] == "shape check failed"


@pytest.mark.parametrize("missing", ["t", "v", "d", "id"])
def test_snapshot_row_missing_one_required_field_is_rejected(missing):
    row = {"t": "Current Demand (MW)", "v": "1,000",
           "d": "1/01/1970 12:00:00 AM EST", "id": "demand"}
    del row[missing]
    body = json.dumps([row]).encode()
    assert validate("Snapshot", body)["error"] == "shape check failed"


def test_snapshot_rejects_a_bad_row_after_a_good_one():
    # `all()` has to look past the first row, not just sample it.
    good = json.loads(GOOD_BODIES["/api/Snapshot"])[0]
    body = json.dumps([good, {"t": "x"}]).encode()
    assert validate("Snapshot", body)["error"] == "shape check failed"


def test_snapshot_rejects_a_non_object_row():
    good = json.loads(GOOD_BODIES["/api/Snapshot"])[0]
    body = json.dumps([good, "not a row"]).encode()
    assert validate("Snapshot", body)["error"] == "shape check failed"


# --- bodies that are not JSON at all ----------------------------------------

def test_html_error_page_is_invalid_json():
    body = b"<!DOCTYPE html><html><body>503 Service Unavailable</body></html>"
    assert validate("FuelMix", body)["error"] == "invalid JSON"


def test_empty_body_is_invalid_json():
    assert validate("FuelMix", b"")["error"] == "invalid JSON"


def test_whitespace_body_is_invalid_json():
    assert validate("FuelMix", b"   \n\t  ")["error"] == "invalid JSON"


def test_invalid_utf8_is_invalid_json():
    # UnicodeDecodeError is a ValueError, so it lands in the same arm.
    assert validate("FuelMix", b"\xff\xfe\x00\x01")["error"] == "invalid JSON"


def test_deeply_nested_body_is_invalid_json_and_raises_nothing():
    # json raises RecursionError, not ValueError, on a body nested this
    # deep. Letting it escape reported a MISO payload as our own bug.
    body = b"[" * 200_000 + b"]" * 200_000
    assert validate("FuelMix", body)["error"] == "invalid JSON"


def test_unparseable_body_still_records_status_and_size():
    body = b"<html>"
    result = validate("FuelMix", body, status=200)
    assert (result["http_status"], result["bytes"]) == (200, len(body))


# --- a ref_id that is present but not a string ------------------------------

def test_numeric_ref_id_is_missing_ref_id():
    body = b'{"RefId": 12345, "TotalMW": "1000", "Fuel": {}}'
    assert validate("FuelMix", body)["error"] == "missing ref_id"


def test_null_ref_id_is_missing_ref_id():
    body = b'{"RefId": null, "TotalMW": "1000", "Fuel": {}}'
    assert validate("FuelMix", body)["error"] == "missing ref_id"


def test_nested_null_ref_id_is_missing_ref_id():
    body = b'{"LoadInfo": {"RefId": null}}'
    assert validate("RealTimeTotalLoad", body)["error"] == "missing ref_id"


def test_windsolar_numeric_ref_id_is_missing_ref_id():
    body = b'{"instance": "s", "RefId": 7, "MktDay": "1970-01-01"}'
    assert validate("WindSolar", body)["error"] == "missing ref_id"


# --- extract_ref_id ---------------------------------------------------------

def test_extract_ref_id_reads_fuelmix_top_level():
    body = {"RefId": "r-1", "TotalMW": "1", "Fuel": {}}
    assert core.extract_ref_id(body, ("RefId",)) == "r-1"


def test_extract_ref_id_reads_load_one_level_down():
    # Reading top level for all four silently yields None here (spec 6.4).
    body = {"LoadInfo": {"RefId": "r-2"}}
    assert core.extract_ref_id(body, ("LoadInfo", "RefId")) == "r-2"


def test_extract_ref_id_reads_windsolar_top_level():
    body = {"instance": "s", "RefId": "r-3", "MktDay": "d"}
    assert core.extract_ref_id(body, ("RefId",)) == "r-3"


def test_extract_ref_id_is_none_for_snapshot():
    assert core.extract_ref_id([{"t": "x"}], None) is None


def test_extract_ref_id_uses_the_live_ref_path_for_every_endpoint():
    for ep in core.ENDPOINTS:
        body = json.loads(GOOD_BODIES[ep.path])
        assert core.extract_ref_id(body, ep.ref_path) == GOOD_REF_IDS[ep.key]


def test_extract_ref_id_is_none_when_a_level_is_not_an_object():
    assert core.extract_ref_id({"LoadInfo": []}, ("LoadInfo", "RefId")) is None


def test_extract_ref_id_is_none_when_the_body_is_not_an_object():
    assert core.extract_ref_id([1, 2, 3], ("RefId",)) is None


def test_extract_ref_id_is_none_when_the_key_is_absent():
    assert core.extract_ref_id({"other": "x"}, ("RefId",)) is None


def test_extract_ref_id_rejects_a_non_string_value():
    assert core.extract_ref_id({"RefId": 12345}, ("RefId",)) is None


# --- the shape predicates on their own --------------------------------------

@pytest.mark.parametrize("shape", [core._shape_fuelmix, core._shape_load,
                                   core._shape_snapshot,
                                   core._shape_windsolar])
@pytest.mark.parametrize("body", [None, 3, "text", [], {}])
def test_no_shape_accepts_a_scalar_or_an_empty_container(shape, body):
    assert shape(body) is False


def test_fuelmix_shape_ignores_extra_keys():
    assert core._shape_fuelmix({"RefId": "r", "TotalMW": "1", "Fuel": {},
                                "Extra": 1}) is True
