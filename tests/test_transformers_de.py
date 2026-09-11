"""transform_de_fueltype: the Data Exchange fuel mix, per region, as prose.

The four legacy transformers have no tests, which is how two bugs in this one
survived review: the newest row was picked by array position rather than by
timestamp, and any region MISO adds later was silently dropped.

These earn their place by protecting behavior, not a coverage number: the
suite measures all of backend, and a line can be covered by a test that would
not notice it changing.
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


# Every entry in _MEASURES, with the exact phrase it must produce. Table-driven
# and exhaustive on purpose: five of the twelve had no assertion at all, and
# mutating "fixed" to a unitless count, or "virtual" to "wind", changed what a
# document claimed MISO published without failing a single test.
MEASURE_CASES = [
    ({"load": 17615.0}, "load 17,615 MW"),
    ({"nsi": -2088.0}, "net scheduled interchange -2,088 MW"),
    ({"supply": 3421.0}, "cleared supply 3,421 MW"),
    ({"fixed": 37271.8}, "fixed demand 37,272 MW"),
    ({"priceSens": 450.5}, "price-sensitive demand 450 MW"),
    ({"virtual": 1200.0}, "virtual demand 1,200 MW"),
    ({"mustRun": 10.0}, "must-run 10 MW"),
    ({"economic": 25128.9}, "economic 25,129 MW"),
    ({"emergency": 3610.8}, "emergency 3,611 MW"),
    ({"loadForecast": 500.0}, "load forecast 500 MW"),
    ({"totalMw": 26858.0}, "total 26,858 MW"),
    ({"unitCount": 2}, "units 2"),
]


def test_every_measure_is_covered_by_a_case():
    """So adding a field to _MEASURES without saying how it reads fails here."""
    from backend.rag.transformers import _MEASURES
    assert {k for values, _ in MEASURE_CASES for k in values} == set(_MEASURES)


@pytest.mark.parametrize("values,expected", MEASURE_CASES)
def test_one_transformer_serves_every_value_shape(values, expected):
    """The eleven endpoints differ in their fields - load, nsi, supply,
    mustRun - which is why they share a generic transformer instead of eleven
    near-identical ones.

    Asserted as equality, not `in`: "total 26,858 MW" is a substring of
    "grand total 26,858 MW", so a relabeled measure passed a containment check.
    """
    assert _measures(de_row("NORTH", **values)) == expected


def test_a_measure_reaches_the_prose_it_is_rendered_for():
    """The table above tests the phrase; this tests that it is actually used."""
    fn = make_de_transformer("thing", URL)
    prose, _, _ = fn({"data": [de_row("NORTH", load=17615.0)]})
    assert "MISO North: load 17,615 MW" in prose


def test_a_null_measure_is_omitted_rather_than_printed_as_zero():
    """7 of 96 rows in a real day-ahead demand payload carry null priceSens.
    "price-sensitive demand 0 MW" is a number MISO never published."""
    assert _measures({"load": 100.0, "priceSens": None}) == "load 100 MW"


def test_a_boolean_in_a_measure_field_is_not_a_quantity():
    """The peak flag is caught by _IGNORED, so that guard alone proves nothing
    about a boolean arriving where a number belongs. True would read "1 MW"."""
    assert _measures({"load": True}) == ""


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


# --- the day, not just the last interval ---------------------------------

def test_the_days_peak_is_reported_not_only_the_final_interval():
    """Found in review: "what was MISO Central's load yesterday?" answered
    41,040 MW, the 23:00 trough, while the day peaked at 54,527 MW at 16:00.
    The peak was in the payload and in no document."""
    fn = make_de_transformer("actual load", URL)
    prose, _, _ = fn({"data": [
        de_row("CENTRAL", when="2026-09-09T16:00:00", load=54527.0),
        de_row("CENTRAL", when="2026-09-09T03:00:00", load=37056.0),
        de_row("CENTRAL", when="2026-09-09T23:00:00", load=41040.0),
    ]})
    assert "load 41,040 MW ending 23:00 EST" in prose        # the end state
    assert "peaked at 54,527 MW (16:00 EST)" in prose        # and the day
    assert "bottomed at 37,056 MW (03:00 EST)" in prose


def test_a_dimensional_feeds_day_range_sums_the_interval():
    """A region's number for an interval is the total across its rows - the
    load forecast splits one region over ten zones - not whichever row is
    first."""
    fn = make_de_transformer("medium-term load forecast", URL)
    prose, _, _ = fn({"data": [
        de_row("NORTH", when="2026-09-09T10:00:00", localResourceZone="Z1", loadForecast=100.0),
        de_row("NORTH", when="2026-09-09T10:00:00", localResourceZone="Z2", loadForecast=200.0),
        de_row("NORTH", when="2026-09-09T11:00:00", localResourceZone="Z1", loadForecast=50.0),
        de_row("NORTH", when="2026-09-09T11:00:00", localResourceZone="Z2", loadForecast=60.0),
    ]})
    assert "peaked at 300 MW (10:00 EST)" in prose
    assert "bottomed at 110 MW (11:00 EST)" in prose


def test_a_flat_measure_is_not_reported_as_a_range():
    """"peaked at 1 and bottomed at 1" is noise, not information."""
    fn = make_de_transformer("fuel on the margin", URL)
    prose, _, _ = fn({"data": [
        de_row("NORTH", when="2026-09-09T08:00:00", fuelType="Hydro", unitCount=1),
        de_row("NORTH", when="2026-09-09T09:00:00", fuelType="Hydro", unitCount=1),
    ]})
    assert "peaked" not in prose


def test_one_interval_is_not_reported_as_a_range():
    fn = make_de_transformer("actual load", URL)
    prose, _, _ = fn({"data": [de_row("NORTH", load=100.0)]})
    assert "Across the day" not in prose


def test_each_region_is_stamped_with_its_own_final_interval():
    """Found in review: regions do not end together. On a real fuel-on-the-
    margin payload CENTRAL ended at 23:55 and NO_REGION at 14:20, and one
    header timestamp was printed over both - 9h35m wrong for the second."""
    fn = make_de_transformer("fuel on the margin", URL)
    prose, as_of, _ = fn({"data": [
        de_row("CENTRAL", when="2026-09-09T23:55:00", fuelType="Coal", unitCount=1),
        de_row("NO_REGION", when="2026-09-09T14:20:00", fuelType="Hydro", unitCount=1),
    ]})
    assert "MISO Central: Coal (units 1) ending 23:55 EST" in prose
    assert "unassigned to a region: Hydro (units 1) ending 14:20 EST" in prose
    # the document's own freshness is still its newest interval
    assert as_of == "2026-09-09T23:55:00"


def test_the_header_does_not_stamp_a_single_time_over_every_region():
    """What the per-region stamp replaced."""
    fn = make_de_transformer("fuel on the margin", URL)
    prose, _, _ = fn({"data": [
        de_row("CENTRAL", when="2026-09-09T23:55:00", unitCount=1),
        de_row("SOUTH", when="2026-09-09T14:20:00", unitCount=1),
    ]})
    header = prose.splitlines()[0]
    assert "23:55" not in header


# --- a forecast is not a settlement --------------------------------------

def test_a_forecast_feed_does_not_call_itself_a_settled_market_day():
    """DELoadForecast is the one forward-looking feed. Telling the reader it
    is "a settled market day, not live output" says the opposite of the truth
    about the only data in the set that has not happened yet."""
    fn = make_de_transformer("medium-term load forecast", URL, "forecast")
    prose, _, _ = fn({"data": [de_row("NORTH", loadForecast=500.0)]})
    assert "forecast MISO published in advance" in prose
    assert "settled market day" not in prose
    assert "completed market day" not in prose


def test_a_settled_feed_still_says_it_is_settled():
    fn = make_de_transformer("actual load", URL)
    prose, _, _ = fn({"data": [de_row("NORTH", load=100.0)]})
    assert "settled market day, not live output" in prose


def test_an_unknown_kind_is_described_as_settled_rather_than_crashing():
    fn = make_de_transformer("actual load", URL, "something-new")
    prose, _, _ = fn({"data": [de_row("NORTH", load=100.0)]})
    assert "completed market day" in prose


# --- row time ------------------------------------------------------------

def test_an_hour_number_is_not_mistaken_for_a_timestamp():
    """timeInterval.value is "1".."24" on the hourly feeds. Sorted as text,
    "9" beats "24", so a payload without `start` would pick hour 9 as newest -
    the same misreading that once made the as-of stamp literally "24"."""
    from backend.rag.transformers import _row_time
    assert _row_time({"timeInterval": {"value": "24"}}) == ""
    assert _row_time({"timeInterval": {"value": "2026-09-09T23:00:00"}}) == \
        "2026-09-09T23:00:00"


def test_a_settled_document_names_the_market_date():
    """The retriever hands Claude the text only - never the as_of metadata -
    so a document that says "ending 23:00 EST" with no day attached lets a
    two-day-old market day read as yesterday."""
    fn = make_de_transformer("actual load", URL)
    prose, _, _ = fn({"data": [de_row("NORTH", load=100.0)]})
    assert "completed market day 2026-09-09" in prose.splitlines()[0]


def test_the_fuel_mix_reports_the_day_as_well_as_its_last_interval():
    """The flagship feed kept its own transformer and was skipped by the first
    pass, so "generation yesterday" understated MISO Central by 30%."""
    from backend.rag.transformers import transform_de_fueltype
    prose, _, _ = transform_de_fueltype({"data": [
        {"timeInterval": {"start": "2026-09-09T16:00:00"}, "region": "CENTRAL",
         "fuelTypes": {"coal": 30000.0}, "totalMw": 50951.0},
        {"timeInterval": {"start": "2026-09-09T23:00:00"}, "region": "CENTRAL",
         "fuelTypes": {"coal": 14357.0}, "totalMw": 35697.0},
    ]})
    assert "35,697 MW total ending 23:00 EST" in prose
    assert "peaked at 50,951 MW (16:00 EST)" in prose
    assert "completed market day 2026-09-09" in prose


def test_a_dimensional_feed_states_the_regions_own_interval_total():
    """Without it the same label named a zone's value and a region's day total
    in one sentence, and the region's final number appeared nowhere: "North
    load forecast at 23:00" would be answered with one zone's 11,045."""
    fn = make_de_transformer("medium-term load forecast", URL, "forecast")
    prose, _, _ = fn({"data": [
        de_row("NORTH", when="2026-09-09T23:00:00", localResourceZone="Z1", loadForecast=11045.0),
        de_row("NORTH", when="2026-09-09T23:00:00", localResourceZone="Z3", loadForecast=6745.0),
        de_row("NORTH", when="2026-09-09T17:00:00", localResourceZone="Z1", loadForecast=21859.0),
    ]})
    assert "17,790 MW load forecast in total" in prose


def test_a_single_row_region_does_not_restate_itself_as_a_total():
    fn = make_de_transformer("actual load", URL)
    prose, _, _ = fn({"data": [de_row("NORTH", load=100.0)]})
    assert "in total" not in prose


def test_a_feed_with_an_unassigned_row_says_it_is_not_one_of_the_three():
    """Four of the six feeds this transformer serves carry a NO_REGION row.
    The fuel-type feed has no such row and says its three regions sum to the
    footprint - that sentence travels, and dropping 11,113 MW of virtual
    demand out of a total is a silent wrong answer."""
    fn = make_de_transformer("day-ahead cleared demand", URL)
    prose, _, _ = fn({"data": [
        de_row("CENTRAL", virtual=100.0),
        de_row("NO_REGION", virtual=11113.0),
    ]})
    assert "not part of North, Central or South" in prose
    assert "never drop it from a total" in prose


def test_a_feed_without_one_stays_quiet_about_it():
    fn = make_de_transformer("actual load", URL)
    prose, _, _ = fn({"data": [de_row("CENTRAL", load=100.0)]})
    assert "Unassigned to a region" not in prose
