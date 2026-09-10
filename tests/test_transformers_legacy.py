"""The four legacy transformers: MISO's JSON in, one prose paragraph out.

These produce the text every live answer is built from, and they had no tests.
Two things matter throughout: MISO returns numbers as strings ("1,000",
"$10.00"), and the "as of" stamp must survive, because an answer that hides its
own staleness is the failure this architecture exists to prevent.
"""

import pytest

from backend.rag.transformers import (
    MISO_DISPLAY_URL,
    _safe_float,
    transform_fuelmix,
    transform_load,
    transform_snapshot,
    transform_windsolar,
)


# --- _safe_float ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1000", 1000.0), ("1,000", 1000.0), ("$10.00", 10.0),
    (" 1,234.5 ", 1234.5), (42, 42.0), (4.5, 4.5),
])
def test_miso_string_numbers_are_parsed(raw, expected):
    """The API returns numbers as strings; doing math on them raw is the bug
    this function exists to prevent.
    """
    assert _safe_float(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "n/a", "--", {}, []])
def test_unparseable_values_fall_back_rather_than_raising(raw):
    assert _safe_float(raw) == 0.0


def test_the_fallback_is_configurable():
    assert _safe_float(None, default=-1.0) == -1.0


# --- fuel mix -------------------------------------------------------------

FUELMIX = {
    "RefId": "10-Sep-2026 - Interval 09:25 EST",
    "TotalMW": "82,059",
    "Fuel": {"Type": [
        {"CATEGORY": "Coal", "ACT": "29,039"},
        {"CATEGORY": "Natural Gas", "ACT": "28,051"},
        {"CATEGORY": "Wind", "ACT": "1,500"},
    ]},
}


def test_fuel_mix_names_every_fuel_with_its_output():
    prose, as_of, url = transform_fuelmix(FUELMIX)
    for fuel in ("Coal", "Natural Gas", "Wind"):
        assert fuel in prose
    assert "29,039" in prose
    assert as_of == FUELMIX["RefId"]
    assert url == MISO_DISPLAY_URL


def test_fuel_mix_reports_shares_of_the_total():
    prose, _, _ = transform_fuelmix(FUELMIX)
    assert "%" in prose


def test_fuel_mix_survives_a_missing_total():
    """A zero total must not divide by zero mid-cycle."""
    prose, _, _ = transform_fuelmix({"Fuel": {"Type": [{"CATEGORY": "Coal", "ACT": "5"}]}})
    assert "Coal" in prose


def test_fuel_mix_survives_no_fuels_at_all():
    prose, as_of, _ = transform_fuelmix({"RefId": "x", "TotalMW": "0"})
    assert isinstance(prose, str) and prose
    assert as_of == "x"


def test_fuel_mix_without_a_refid_still_produces_prose():
    prose, as_of, _ = transform_fuelmix({"TotalMW": "10", "Fuel": {"Type": []}})
    assert isinstance(as_of, str)
    assert prose


# --- load -----------------------------------------------------------------

# Shapes taken from real data/raw/ payloads: MediumTermLoadForecast and
# FiveMinTotalLoad are lists of one-key wrappers, not dicts.
LOAD = {"LoadInfo": {
    "RefId": "10-Sep-2026 - Interval 09:25 EST",
    "ClearedMW": [{"ClearedMWHourly": {"HourEndEST": "10", "Value": "84,172"}}],
    "MediumTermLoadForecast": [
        {"Forecast": {"HourEndEST": "10", "LoadForecast": "85,419"}}],
    "FiveMinTotalLoad": [{"Load": {"Time": "09:25", "Value": "84,172"}}],
}}


def test_load_produces_prose_and_keeps_its_as_of():
    prose, as_of, url = transform_load(LOAD)
    assert isinstance(prose, str) and prose
    assert "09:25" in as_of
    assert url == MISO_DISPLAY_URL


def test_load_survives_an_empty_payload():
    prose, _, _ = transform_load({})
    assert isinstance(prose, str) and prose


def test_load_survives_missing_inner_sections():
    prose, _, _ = transform_load({"LoadInfo": {"RefId": "x"}})
    assert isinstance(prose, str) and prose


# --- snapshot -------------------------------------------------------------

SNAPSHOT = [
    {"t": "Forecasted Peak Demand", "v": "107,605", "d": "MW", "id": "1"},
    {"t": "Current Demand", "v": "104,892", "d": "MW", "id": "2"},
    {"t": "Marginal Energy Cost", "v": "$56.23", "d": "$/MWh", "id": "3"},
]


def test_snapshot_reports_each_labelled_value():
    prose, _, url = transform_snapshot(SNAPSHOT)
    assert "Forecasted Peak Demand" in prose or "Peak Demand" in prose
    assert "107,605" in prose or "107605" in prose
    assert url == MISO_DISPLAY_URL


def test_snapshot_survives_an_empty_list():
    prose, _, _ = transform_snapshot([])
    assert isinstance(prose, str) and prose


def test_snapshot_survives_rows_missing_fields():
    prose, _, _ = transform_snapshot([{"t": "Only a label"}])
    assert isinstance(prose, str) and prose


# --- wind and solar -------------------------------------------------------

# "instance" is a list of intervals, each carrying both forecast and actual.
WINDSOLAR = {
    "instance": [
        {"ForecastDateTimeEST": "2026-09-10 09:00:00 AM", "ForecastHourEndingEST": "9",
         "ForecastWindValue": "5740.00", "ForecastSolarValue": "1200.00",
         "ActualDateTimeEST": "2026-09-10 09:00:00 AM", "ActualHourEndingEST": "9",
         "ActualWindValue": "1500.00", "ActualSolarValue": "2596.00"},
    ],
    "RefId": "10-Sep-2026 - Interval 09:00 EST",
    "MktDay": "09-10-2026",
}


def test_wind_and_solar_produce_prose_with_an_as_of():
    prose, as_of, url = transform_windsolar(WINDSOLAR)
    assert isinstance(prose, str) and prose
    assert "09:00" in as_of
    assert url == MISO_DISPLAY_URL


def test_wind_and_solar_survive_an_empty_payload():
    prose, _, _ = transform_windsolar({})
    assert isinstance(prose, str) and prose


def test_wind_and_solar_survive_no_intervals():
    prose, _, _ = transform_windsolar({"RefId": "x", "instance": []})
    assert isinstance(prose, str) and prose


def test_wind_and_solar_ignore_intervals_with_no_actuals_yet():
    """Later hours of the day carry a forecast but no actual value."""
    prose, _, _ = transform_windsolar({"RefId": "x", "instance": [
        {"ForecastDateTimeEST": "2026-09-10 11:00:00 PM", "ForecastWindValue": "900.00",
         "ActualDateTimeEST": None, "ActualWindValue": None},
    ]})
    assert isinstance(prose, str) and prose


# --- shared contract ------------------------------------------------------

@pytest.mark.parametrize("fn,payload", [
    (transform_fuelmix, FUELMIX),
    (transform_load, LOAD),
    (transform_snapshot, SNAPSHOT),
    (transform_windsolar, WINDSOLAR),
])
def test_every_transformer_returns_prose_as_of_and_a_source_url(fn, payload):
    """ingest_api unpacks exactly three values from each of these."""
    result = fn(payload)
    assert len(result) == 3
    prose, as_of, url = result
    assert isinstance(prose, str) and prose
    assert isinstance(as_of, str)
    assert url.startswith("https://www.misoenergy.org")


@pytest.mark.parametrize("fn,empty", [
    (transform_fuelmix, {}),
    (transform_load, {}),
    (transform_snapshot, []),
    (transform_windsolar, {}),
])
def test_no_transformer_raises_on_an_empty_payload(fn, empty):
    """A malformed cycle must degrade to thin prose, never take the poller down."""
    fn(empty)
