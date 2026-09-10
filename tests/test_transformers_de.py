"""transform_de_fueltype: the Data Exchange fuel mix, per region, as prose.

The four legacy transformers have no tests, which is how two bugs in this one
survived review: the newest row was picked by array position rather than by
timestamp, and any region MISO adds later was silently dropped.

Outside the coverage gate (pytest.ini measures backend.poller only), so these
earn their place by protecting behavior, not a number.
"""

import pytest

from backend.rag.transformers import (
    DATA_EXCHANGE_DOC_URL,
    _latest_row_per_region,
    transform_de_fueltype,
)


def row(region, total, when="2026-09-10 09:25:00.000", **fuels):
    base = {"coal": 0, "gas": 0, "nuclear": 0, "water": 0,
            "wind": 0, "solar": 0, "storage": 0, "other": 0}
    base.update(fuels)
    return {"timeInterval": {"start": when, "value": when},
            "region": region, "fuelTypes": base, "totalMw": total}


# --- picking the current interval ----------------------------------------

def test_the_newest_interval_wins_regardless_of_array_order():
    """MISO does not document row ordering. Trusting position would report the
    oldest interval of the day as "right now" if the API returned newest-first.
    """
    rows = [row("NORTH", 999, when="2026-09-10 09:25:00.000"),
            row("NORTH", 111, when="2026-09-10 08:00:00.000")]
    assert _latest_row_per_region(rows)["NORTH"]["totalMw"] == 999


def test_the_newest_interval_wins_when_it_comes_last_too():
    rows = [row("NORTH", 111, when="2026-09-10 08:00:00.000"),
            row("NORTH", 999, when="2026-09-10 09:25:00.000")]
    assert _latest_row_per_region(rows)["NORTH"]["totalMw"] == 999


def test_rows_without_a_region_are_ignored():
    assert _latest_row_per_region([{"totalMw": 5}, "junk", None]) == {}


# --- prose ----------------------------------------------------------------

def test_every_region_is_named_with_its_total():
    prose, _, _ = transform_de_fueltype({"data": [
        row("NORTH", 30000, coal=12000, wind=4000),
        row("SOUTH", 25000, gas=18000),
    ]})
    assert "MISO North" in prose and "30,000 MW" in prose
    assert "MISO South" in prose and "25,000 MW" in prose


def test_the_footprint_total_is_flagged_so_it_is_not_added_to_the_regions():
    """MISO + NORTH + CENTRAL + SOUTH double-counts. The model is told, because
    whether MISO returns that row unfiltered is still unverified.
    """
    prose, _, _ = transform_de_fueltype({"data": [row("MISO", 90000, coal=50000)]})
    assert "do not add it" in prose


def test_an_unknown_region_is_reported_rather_than_dropped():
    """MISO's enum is documented closed, but a silently missing region is worse
    than an unpolished name.
    """
    prose, _, _ = transform_de_fueltype({"data": [row("WEST", 4242, wind=4242)]})
    assert "WEST" in prose and "4,242" in prose


def test_shares_are_percentages_of_that_region_total():
    prose, _, _ = transform_de_fueltype({"data": [row("NORTH", 1000, coal=250)]})
    assert "25.0%" in prose


def test_a_zero_total_does_not_divide_by_zero():
    prose, _, _ = transform_de_fueltype({"data": [row("NORTH", 0, coal=0)]})
    assert "MISO North" in prose and "%" not in prose


def test_missing_fuel_types_still_produce_a_line():
    prose, _, _ = transform_de_fueltype(
        {"data": [{"region": "NORTH", "totalMw": 700, "timeInterval": {}}]})
    assert "MISO North: 700 MW total." in prose


def test_non_numeric_values_are_treated_as_zero():
    prose, _, _ = transform_de_fueltype({"data": [row("NORTH", "1,000", coal="250")]})
    assert "1,000 MW total" in prose and "25.0%" in prose


@pytest.mark.parametrize("payload", [{}, {"data": []}, {"data": None}, "nonsense"])
def test_no_data_says_so_instead_of_inventing(payload):
    prose, as_of, url = transform_de_fueltype(payload)
    assert "No MISO Data Exchange generation data" in prose
    assert as_of == ""
    assert url == DATA_EXCHANGE_DOC_URL


def test_the_as_of_time_comes_from_the_interval():
    _, as_of, _ = transform_de_fueltype({"data": [row("NORTH", 1, when="2026-09-10 09:25:00.000")]})
    assert as_of == "2026-09-10 09:25:00.000"


def test_the_as_of_is_a_timestamp_not_the_hour_number():
    """timeInterval.value is "1".."24", the hour of the market day. Preferring
    it over start produced an as-of of "24"."""
    payload = {"data": [{"region": "NORTH", "totalMw": 100, "fuelTypes": {"wind": 100},
                         "timeInterval": {"start": "2026-09-09T23:00:00",
                                          "end": "2026-09-10T00:00:00", "value": "24"}}]}
    _, as_of, _ = transform_de_fueltype(payload)
    assert as_of == "2026-09-09T23:00:00"


def test_the_prose_says_the_day_is_settled_not_live():
    """Rule 5: staleness stays visible. This feed is never "right now", and the
    /real-time/ path in the URL invites exactly that misreading."""
    prose, _, _ = transform_de_fueltype({"data": [row("NORTH", 100, wind=100)]})
    assert "completed market day" in prose
    assert "not live output" in prose


def test_the_footprint_warning_only_appears_when_a_footprint_row_does():
    """MISO returns the three regions and no MISO-wide row, so warning about
    double-counting one would describe data that is not there."""
    three, _, _ = transform_de_fueltype({"data": [
        row("NORTH", 1), row("CENTRAL", 1), row("SOUTH", 1)]})
    assert "MISO overall" not in three
    assert "three regions" in three
    withtotal, _, _ = transform_de_fueltype({"data": [row("MISO", 3), row("NORTH", 1)]})
    assert "do not add it" in withtotal


def test_the_source_url_is_the_operation_that_produced_it():
    _, _, url = transform_de_fueltype({"data": [row("NORTH", 1)]})
    assert url == DATA_EXCHANGE_DOC_URL
    assert "generation-fuel-type" in url
