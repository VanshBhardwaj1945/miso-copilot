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


def test_the_snapshot_as_of_is_the_newest_row_not_the_first():
    """Row zero is "Forecasted Peak Demand", stamped midnight. Taking it
    labeled an answer about 8pm demand "12:00:00 AM" - which was invisible
    while the value was discarded and is on screen now that it is not."""
    from backend.rag.transformers import transform_snapshot
    rows = [
        {"t": "Forecasted Peak Demand (MW)", "v": "98,729", "d": "9/10/2026 12:00:00 AM EST"},
        {"t": "Current Demand (MW)", "v": "96,135", "d": "9/10/2026 8:00:00 PM EST"},
        {"t": "Scheduled Imports (MW)", "v": "-4,561", "d": "9/10/2026 7:58:00 PM EST"},
    ]
    assert transform_snapshot(rows)[1] == "9/10/2026 8:00:00 PM EST"


def test_an_unparseable_snapshot_stamp_falls_back_rather_than_crashing():
    from backend.rag.transformers import transform_snapshot
    assert transform_snapshot([{"t": "x", "v": "1", "d": "not a date"}])[1] == "not a date"
    assert transform_snapshot([])[1] == "Recent Interval"


def test_the_load_forecast_reports_the_days_peak_not_hour_one():
    """forecast[0] is Hour Ending 1 - 1 AM - and it read as "the forecast"
    printed beside an 8 PM actual."""
    from backend.rag.transformers import transform_load
    prose, _, _ = transform_load({"LoadInfo": {
        "RefId": "10-Sep-2026 - Interval 20:25 EST",
        "FiveMinTotalLoad": [{"Load": {"Time": "20:25", "Value": "95139"}}],
        "MediumTermLoadForecast": [
            {"Forecast": {"HourEnding": "1", "LoadForecast": "78103"}},
            {"Forecast": {"HourEnding": "17", "LoadForecast": "98729"}},
        ]}})
    assert "Day-Ahead Forecast Peak: 98,729 MW in the hour ending 5 PM EST" in prose
    assert "78,103" not in prose


def test_the_solar_forecast_is_the_peak_not_midnight():
    """instances[0] is midnight, so this printed "Day-Ahead Forecasted Solar:
    0.0 MW" directly under an evening actual of 2,074 MW."""
    from backend.rag.transformers import transform_windsolar
    prose, _, _ = transform_windsolar({"RefId": "r", "instance": [
        {"ActualDateTimeEST": "2026-09-10 12:00:00 AM", "ActualWindValue": "100",
         "ActualSolarValue": "0", "ForecastDateTimeEST": "2026-09-10 00:00:00",
         "ForecastHourEndingEST": "1", "ForecastWindValue": "200",
         "ForecastSolarValue": "0"},
        {"ActualDateTimeEST": "2026-09-10 1:00:00 PM", "ActualWindValue": "300",
         "ActualSolarValue": "2074", "ForecastDateTimeEST": "2026-09-10 12:00:00",
         "ForecastHourEndingEST": "13", "ForecastWindValue": "400",
         "ForecastSolarValue": "15687"},
    ]})
    assert "Forecast Peak Solar, 2026-09-10: 15,687.0 MW" in prose
    assert "Forecasted Solar: 0.0 MW" not in prose


def test_the_wind_forecast_peak_is_reported_per_day():
    """The feed carries 48 rows - today and tomorrow. One peak across both
    reported tomorrow's 19,904 MW as today's, and a grid operator knows their
    own forecast."""
    from backend.rag.transformers import transform_windsolar
    prose, _, _ = transform_windsolar({"RefId": "r", "instance": [
        {"ForecastDateTimeEST": "2026-09-10 23:00:00", "ForecastHourEndingEST": "24",
         "ForecastWindValue": "13561", "ForecastSolarValue": "0"},
        {"ForecastDateTimeEST": "2026-09-11 22:00:00", "ForecastHourEndingEST": "23",
         "ForecastWindValue": "19904", "ForecastSolarValue": "0"},
    ]})
    assert "Forecast Peak Wind, 2026-09-10: 13,561.0 MW, in the hour ending midnight EST" in prose
    assert "Forecast Peak Wind, 2026-09-11: 19,904.0 MW, in the hour ending 11 PM EST" in prose
    assert "two forecast days" in prose


def test_the_fuel_mix_total_is_not_called_generation():
    """Imports is a line item inside TotalMW. Read as generation, it invited
    adding imports on top - which answered a Maximum Generation risk question
    with "covering load with room to spare" while supply was under load."""
    from backend.rag.transformers import transform_fuelmix
    prose, _, _ = transform_fuelmix({"RefId": "r", "TotalMW": "91,307", "Fuel": {"Type": [
        {"CATEGORY": "Coal", "ACT": "33,745"},
        {"CATEGORY": "Imports", "ACT": "3,355"},
    ]}})
    assert "Total Supply (own generation plus imports): 91,307 MW" in prose
    assert "Total Grid Generation" not in prose
    assert "already counted inside the total supply" in prose


def test_the_fuel_type_footprint_claim_is_scoped_to_its_own_feed():
    """It used to read as a general rule about MISO's regions, and the other
    Data Exchange feeds do carry an unassigned row."""
    from backend.rag.transformers import transform_de_fueltype
    prose, _, _ = transform_de_fueltype({"data": [
        {"timeInterval": {"start": "2026-09-09T23:00:00"}, "region": "NORTH",
         "fuelTypes": {"coal": 100.0}, "totalMw": 100.0},
    ]})
    assert "footprint total for this fuel-type feed" in prose
    assert "do not carry this sentence over to them" in prose


@pytest.mark.parametrize("hour,expected", [
    ("1", "1 AM"), ("11", "11 AM"), ("12", "noon"), ("13", "1 PM"),
    ("17", "5 PM"), ("23", "11 PM"), ("24", "midnight"),
])
def test_an_hour_ending_reads_as_a_clock_time(hour, expected):
    """MISO counts hours 1-24. "HE 24" is trading-floor shorthand and "hour
    ending 24" reads like a 24th hour no clock has."""
    from backend.rag.transformers import _hour_ending
    assert _hour_ending(hour) == expected


@pytest.mark.parametrize("bad", ["", "0", "25", "abc", None])
def test_an_unusable_hour_is_omitted_rather_than_guessed(bad):
    from backend.rag.transformers import _hour_ending
    assert _hour_ending(bad) == ""
