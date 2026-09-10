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


# --- the generic region transformer ---------------------------------------

from backend.rag.transformers import (  # noqa: E402
    _measures,
    make_de_transformer,
)

URL = "https://data-exchange.misoenergy.org/x"


def de_row(region, when="2026-09-09T23:00:00", **values):
    return {"timeInterval": {"start": when, "end": when, "value": "24"},
            "region": region, **values}


def test_one_transformer_serves_every_value_shape():
    """The eleven endpoints differ in their fields - load, nsi, supply,
    mustRun - which is why they share a generic transformer instead of eleven
    near-identical ones."""
    for values, expected in (
        ({"load": 17615.0}, "load 17,615 MW"),
        ({"nsi": -2088.0}, "net scheduled interchange -2,088 MW"),
        ({"supply": 3421.0}, "cleared supply 3,421 MW"),
        ({"mustRun": 10.0, "economic": 20.0}, "must-run 10 MW"),
        ({"loadForecast": 500.0}, "load forecast 500 MW"),
    ):
        fn = make_de_transformer("thing", URL)
        prose, _, _ = fn({"data": [de_row("NORTH", **values)]})
        assert expected in prose, values


def test_a_nested_fuel_breakdown_is_flattened():
    fn = make_de_transformer("day-ahead generation by fuel type", URL)
    prose, _, _ = fn({"data": [de_row("SOUTH", fuelTypes={"wind": 228.0, "coal": 0},
                                      totalMw=26858.0)]})
    assert "wind 228 MW" in prose
    assert "coal" not in prose          # zero values are noise, not information
    assert "total 26,858 MW" in prose


def test_only_known_measurements_are_reported():
    """region/interval/init are row bookkeeping. An unknown field is skipped
    rather than guessed at - inventing a unit is how "units 2 MW" happened."""
    assert _measures({"region": "NORTH", "interval": "24", "init": "5",
                      "load": 100.0, "somethingNew": 7}) == "load 100 MW"


def test_a_count_is_not_reported_in_megawatts():
    """unitCount is a number of generators. Printing "units 2 MW" is a
    confidently wrong figure, which is worse than a missing one."""
    assert _measures({"unitCount": 2}) == "units 2"


def test_a_boolean_is_never_a_quantity():
    """peak is a flag; it rendered as "peak 0 MW" before."""
    assert _measures({"peak": False, "load": 5.0}) == "load 5 MW"


def test_the_fuel_on_the_margin_names_the_fuel():
    """The whole point of that endpoint. fuelType was being skipped, so every
    region reported a count with no indication of which fuel it counted."""
    fn = make_de_transformer("fuel on the margin", URL)
    prose, _, _ = fn({"data": [
        de_row("NORTH", fuelType="Coal", unitCount=1),
        de_row("NORTH", fuelType="Gas", unitCount=2),
    ]})
    assert "Coal (units 1)" in prose and "Gas (units 2)" in prose
    assert "MW" not in prose.split("MISO North")[1].split("\n")[0]


def test_rows_sharing_a_region_are_all_kept():
    """Keeping one row per region answered "which fuel is on the margin?" with
    a single arbitrary fuel."""
    fn = make_de_transformer("fuel on the margin", URL)
    prose, _, _ = fn({"data": [de_row("NORTH", fuelType=f, unitCount=1)
                               for f in ("Coal", "Gas", "Nuclear")]})
    for fuel in ("Coal", "Gas", "Nuclear"):
        assert fuel in prose


def test_a_local_resource_zone_is_kept_as_a_label():
    """The forecast carries a zone per row; dropping it collapsed nine zones
    into one arbitrary number per region."""
    fn = make_de_transformer("load forecast", URL)
    prose, _, _ = fn({"data": [
        de_row("NORTH", localResourceZone="Z1", loadForecast=11045.0),
        de_row("NORTH", localResourceZone="Z3", loadForecast=6745.0),
    ]})
    assert "Z1 (load forecast 11,045 MW)" in prose
    assert "Z3 (load forecast 6,745 MW)" in prose


def test_older_intervals_do_not_leak_in():
    """Only the newest interval's rows, even though several rows share it."""
    fn = make_de_transformer("fuel on the margin", URL)
    prose, as_of, _ = fn({"data": [
        de_row("NORTH", when="2026-09-09T01:00:00", fuelType="Oil", unitCount=9),
        de_row("NORTH", when="2026-09-09T23:00:00", fuelType="Coal", unitCount=1),
        de_row("NORTH", when="2026-09-09T23:00:00", fuelType="Gas", unitCount=2),
    ]})
    assert "Oil" not in prose
    assert "Coal" in prose and "Gas" in prose
    assert as_of == "2026-09-09T23:00:00"


def test_the_newest_interval_wins_here_too():
    fn = make_de_transformer("actual load", URL)
    prose, as_of, _ = fn({"data": [
        de_row("NORTH", when="2026-09-09T01:00:00", load=1.0),
        de_row("NORTH", when="2026-09-09T23:00:00", load=999.0),
    ]})
    assert "load 999 MW" in prose
    assert "load 1 MW" not in prose
    assert as_of == "2026-09-09T23:00:00"


def test_every_region_present_is_reported():
    fn = make_de_transformer("actual load", URL)
    prose, _, _ = fn({"data": [de_row(r, load=1.0)
                               for r in ("NORTH", "CENTRAL", "SOUTH")]})
    for name in ("MISO North", "MISO Central", "MISO South"):
        assert name in prose


def test_the_prose_says_the_day_is_settled():
    fn = make_de_transformer("actual load", URL)
    prose, _, _ = fn({"data": [de_row("NORTH", load=1.0)]})
    assert "completed market day" in prose and "not live output" in prose


def test_a_region_with_no_numeric_fields_still_appears():
    fn = make_de_transformer("thing", URL)
    prose, _, _ = fn({"data": [de_row("NORTH")]})
    assert "no values reported" in prose


@pytest.mark.parametrize("payload", [{}, {"data": []}, {"data": None}, "nonsense"])
def test_no_data_says_so(payload):
    fn = make_de_transformer("actual load", URL)
    prose, as_of, url = fn(payload)
    assert "No MISO Data Exchange data is available for actual load" in prose
    assert as_of == "" and url == URL


def test_the_title_names_the_endpoint():
    fn = make_de_transformer("day-ahead net scheduled interchange", URL)
    prose, _, _ = fn({"data": [de_row("NORTH", nsi=1.0)]})
    assert "day-ahead net scheduled interchange" in prose
